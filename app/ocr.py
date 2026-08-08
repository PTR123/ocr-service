"""RapidOCR 单例封装。

模型只在进程启动时加载一次（常驻内存），所有请求复用同一个实例，
避免每个请求重复加载模型（加载慢、耗内存）。
"""
import threading

from rapidocr_onnxruntime import RapidOCR

_engine: RapidOCR | None = None
_lock = threading.Lock()


def get_engine() -> RapidOCR:
    """获取全局唯一的 RapidOCR 实例（线程安全懒加载）。"""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                # RapidOCR() 首次实例化会加载三个 ONNX 模型（检测/方向/识别）
                _engine = RapidOCR()
    return _engine
