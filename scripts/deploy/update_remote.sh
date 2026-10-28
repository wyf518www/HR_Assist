#!/bin/sh
# Run as the existing administrator. No package installation, downloads, or pruning.
set -eu
umask 077
if [ "$(id -u)" != 0 ]; then
    exec sudo sh "$0" "$@"
fi
[ "$#" = 5 ] || { echo 'Usage: update_remote.sh PROJECT SERVICE IMAGE RELEASE SHA256'; exit 2; }
project=$1 service=$2 image=$3 release=$4 digest=$5
case "$project" in /*) ;; *) echo 'Project must be absolute.'; exit 2;; esac
case "$service:$image:$release:$digest" in *[!a-zA-Z0-9_.:/-]*) echo 'Invalid argument.'; exit 2;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd -- "$project"
project=$(pwd -P)
[ -d hr_assist ] && [ ! -L hr_assist ] || { echo 'Expected an ordinary hr_assist source directory.'; exit 1; }
# sudo's non-login shell on DSM may omit the Container Manager binary directory.
# Resolve once, then use this absolute executable for the entire update.
docker_bin=$(command -v docker 2>/dev/null || true)
case "$docker_bin" in /*) [ -x "$docker_bin" ] || docker_bin='' ;; *) docker_bin='' ;; esac
if [ -z "$docker_bin" ]; then
    for candidate in \
        "/usr/local/bin/docker" \
        "/var/packages/ContainerManager/target/usr/bin/docker" \
        "/var/packages/Docker/target/usr/bin/docker" \
        "/usr/bin/docker" \
        "/bin/docker"; do
        if [ -x "$candidate" ] && [ ! -d "$candidate" ]; then
            docker_bin=$candidate
            break
        fi
    done
fi
[ -n "$docker_bin" ] || { echo 'Docker CLI not found in PATH or standard DSM package locations. Check that Container Manager is installed. No service was changed.'; exit 1; }
docker_dir=${docker_bin%/*}
PATH="$docker_dir:${PATH:-/usr/bin:/bin}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
docker() { "$docker_bin" "$@"; }
echo "Docker CLI: $docker_bin"
docker --version || { echo 'Docker CLI could not run. No service was changed.'; exit 1; }
compose_mode=plugin
compose_bin=''
if ! "$docker_bin" compose version >/dev/null 2>&1; then
    # Some DSM packages expose Compose v2 as a standalone executable instead
    # of a plugin visible to root. Do not silently use an older Compose v1.
    path_compose=$(command -v docker-compose 2>/dev/null || true)
    for candidate in "$path_compose" \
        "$docker_dir/docker-compose" \
        "/var/packages/ContainerManager/target/usr/bin/docker-compose" \
        "/var/packages/ContainerManager/target/usr/lib/docker/cli-plugins/docker-compose" \
        "/var/packages/Docker/target/usr/bin/docker-compose" \
        "/var/packages/Docker/target/usr/lib/docker/cli-plugins/docker-compose" \
        "/usr/local/lib/docker/cli-plugins/docker-compose" \
        "/usr/local/libexec/docker/cli-plugins/docker-compose" \
        "/usr/lib/docker/cli-plugins/docker-compose" \
        "/usr/libexec/docker/cli-plugins/docker-compose" \
        "/usr/local/bin/docker-compose" \
        "/usr/bin/docker-compose"; do
        case "$candidate" in /*) ;; *) continue ;; esac
        [ -x "$candidate" ] && [ ! -d "$candidate" ] || continue
        compose_version=$("$candidate" version --short 2>/dev/null) || continue
        case "$compose_version" in 2.*|v2.*) compose_bin=$candidate; break ;; esac
    done
    [ -n "$compose_bin" ] || { echo 'Docker Compose v2 is unavailable in this sudo environment. Check the Container Manager installation. No service was changed.'; exit 1; }
    compose_mode=standalone
fi
compose() {
    if [ "$compose_mode" = plugin ]; then
        "$docker_bin" compose "$@"
    else
        "$compose_bin" "$@"
    fi
}
if [ "$compose_mode" = plugin ]; then
    echo "Compose command: $docker_bin compose"
else
    echo "Compose command: $compose_bin"
fi
compose version
docker info >/dev/null 2>&1 || { echo 'Docker engine is unreachable. Check that Container Manager is running and this administrator can access Docker. No service was changed.'; exit 1; }
lock="$project/.hr-assist-update.lock"
mkdir "$lock" 2>/dev/null || { echo 'Another update owns the lock. Check the NAS before retrying; do not remove a live lock.'; exit 1; }
stopped=0 new_started=0 healthy=0 retagged=0 old_container=''
work="$project/.hr-assist-updates/$release"
finish() {
    result=$?
    trap - EXIT HUP INT TERM
    set +e
    if [ "$result" -ne 0 ]; then
        if [ "$new_started" = 1 ] && [ "$healthy" = 0 ]; then
            if compose stop -t 60 "$service" >/dev/null 2>&1; then
                echo 'UPDATE FAILED after activation. New service stopped; data was NOT overwritten or rolled back.'
            else
                echo 'UPDATE FAILED after activation. Stop request also failed; check container status immediately. Data was NOT rolled back.'
            fi
            echo "Preserve current data and review the backup at: $work/backup"
        elif [ "$stopped" = 1 ] && [ "$new_started" = 0 ]; then
            if [ "$retagged" = 1 ]; then
                docker image tag "$old_image" "$image" || echo 'Could not restore the original image tag; review the saved backup image.'
            fi
            docker start "$old_container" >/dev/null 2>&1 || true
            echo 'Update stopped before activation; restart of the original container was requested.'
        elif [ "$healthy" = 1 ]; then
            echo 'New service is healthy, but final source synchronization did not finish. Check the update directory.'
        fi
        [ ! -d "$work" ] || printf '%s\n' FAILED > "$work/status"
    fi
    rmdir "$lock" 2>/dev/null || true
    exit "$result"
}
trap finish EXIT
trap 'exit 130' HUP INT TERM
mkdir -p "$project/.hr-assist-updates"
chmod 700 "$project/.hr-assist-updates"
mkdir "$work"
# Backup/config snapshots must never be picked up by a later git add on the NAS.
if [ -d "$project/.git/info" ]; then
    for pattern in '/.hr-assist-updates/' '/.hr-assist-update.lock/' '/.hr-assist-source-*/'; do
        grep -qxF "$pattern" "$project/.git/info/exclude" 2>/dev/null || printf '%s\n' "$pattern" >> "$project/.git/info/exclude"
    done
fi
printf '%s\n' PREPARING > "$work/status"
cp "$script_dir/package.tar.gz" "$script_dir/update_helper.py" "$work/"
old_container=$(compose ps -q "$service")
[ -n "$old_container" ] || { echo 'No running service was found.'; exit 1; }
old_image=$(docker inspect --format '{{.Image}}' "$old_container")
base="hr-assist:backup-$release"
candidate="hr-assist:update-$release"
docker image tag "$old_image" "$base"
docker inspect "$old_container" > "$work/container.json"
compose config --format json > "$work/compose.json"
printf '%s\n' "$base" > "$work/old-image-tag"
helper() {
    docker run --rm --network none --user 0:0 --read-only --tmpfs /tmp:rw,size=32m \
        -v "$work:/update" -v "$project:/project:ro" \
        --entrypoint python "$base" /update/update_helper.py "$@"
}
helper prepare --project "$project" --service "$service" --image "$image" --sha256 "$digest" --base-tag "$base"
state_path=$(cat "$work/state-path")
data_path=$(cat "$work/data-path")
secret_path=$(cat "$work/secret-path")
data_helper() {
    docker run --rm --network none --user 0:0 --read-only --tmpfs /tmp:rw,size=32m \
        -v "$work:/update" -v "$project:/project:ro" -v "$state_path:/state:ro" \
        -v "$data_path:/data:ro" -v "$secret_path:/admin-secret:ro" \
        --entrypoint python "$base" /update/update_helper.py "$@"
}
docker exec -i "$old_container" python - idle < "$work/update_helper.py"
data_helper capacity
echo 'Building the candidate image from the exact running image (no apt/pip/pull).'
printf '%s\n' BUILDING > "$work/status"
docker build --pull=false --network none -f "$work/context/Dockerfile" -t "$candidate" "$work/context"
docker run --rm --network none --read-only --tmpfs /tmp:rw,size=32m --entrypoint python "$candidate" \
    -c 'from hr_assist.main import create_app; from hr_assist import __version__; print("Candidate imports OK:", __version__)'
docker exec -i "$old_container" python - idle < "$work/update_helper.py"
echo 'Stopping the service, then backing up actual state/data mounts. Do not use the workbench during this step.'
printf '%s\n' BACKUP > "$work/status"
stopped=1
compose stop -t 360 "$service"
[ "$(docker inspect --format '{{.State.Running}}' "$old_container")" = false ]
data_helper backup
[ -f "$work/backup/COMPLETE" ]
echo 'Activating the new image. Backups and the old image are retained.'
retagged=1
docker image tag "$candidate" "$image"
printf '%s\n' ACTIVATING > "$work/status"
new_started=1
compose up -d --no-build --pull never --force-recreate "$service"
new_container=$(compose ps -q "$service")
expected_image=$(docker image inspect --format '{{.Id}}' "$candidate")
[ "$(docker inspect --format '{{.Image}}' "$new_container")" = "$expected_image" ]
attempt=0
while [ "$attempt" -lt 36 ]; do
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$new_container")
    if [ "$status" = healthy ]; then
        healthy=1
        break
    fi
    [ "$status" != unhealthy ] || { echo 'Container reported unhealthy.'; exit 1; }
    attempt=$((attempt + 1))
    [ "$((attempt % 6))" != 0 ] || echo 'Waiting for the container health check...'
    sleep 5
done
[ "$healthy" = 1 ] || { echo 'Health check timed out.'; exit 1; }
# Replace source only after health succeeds; preserve the entire old directory.
cp -a "$work/context/hr_assist" "$project/.hr-assist-source-$release"
mv "$project/hr_assist" "$work/source-before"
if ! mv "$project/.hr-assist-source-$release" "$project/hr_assist"; then
    mv "$work/source-before" "$project/hr_assist"
    exit 1
fi
docker exec "$new_container" python -c 'from hr_assist import __version__; print("RUNNING_VERSION:", __version__)'
printf '%s\n' SUCCESS > "$work/status"
echo "UPDATE SUCCESS. Container is healthy. Backup: $work/backup"
