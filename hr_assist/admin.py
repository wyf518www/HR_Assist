"""Offline password reset. Stop the service and replace the mounted secret first."""
from .config import Environment
from .db import Database
from .instance import InstanceLock
from .security import password_hash


def main():
    env=Environment.read()
    if len(env.admin_password)<12 or env.admin_password.startswith('CHANGE_ME'):
        raise SystemExit('请提供至少12位新密码。')
    lock=InstanceLock(env.state_dir)
    try:
        db=Database(env.state_dir)
        db.put('admin',{'username':env.admin_username,'hash':password_hash(env.admin_password)})
        db.execute('DELETE FROM sessions')
        db.event('ADMIN_RESET','管理员凭据已离线重置，全部会话失效。')
        print('管理员凭据已更新。')
    finally:
        lock.close()


if __name__=='__main__':main()
