"""GUI 线程投递装饰器（main 与各窗口模块共用）。"""
import functools


def _on_gui(method):
    """把整个方法体调度到 GUI 线程执行

    pystray 的菜单回调是在托盘消息循环线程里同步跑的，在那里直接
    root.mainloop() 会把托盘卡死（右键不出菜单）。所以所有窗口都必须
    投递到 GUI 线程创建。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._ui(lambda: method(self, *args, **kwargs))
    return wrapper
