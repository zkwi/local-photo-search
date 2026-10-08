"""选 GPU 还是 CPU：锁定的 PyTorch（CUDA 13 版）只支持 Turing（GTX 16 / RTX 20）及更新的显卡，老显卡要自动改用 CPU。
用假的 torch 模拟各种显卡，不需要真的显卡。"""
from backend.common import pick_device


class FakeTensor:
    def add_(self, _):
        return self

    def item(self):
        return 1.0


class FakeTorch:
    def __init__(self, available=True, capability=(8, 9), kernels_work=True):
        self.cuda = self
        self._available, self._capability, self._works = available, capability, kernels_work

    def is_available(self):
        return self._available

    def get_device_capability(self):
        return self._capability

    def zeros(self, *_, device=None):
        if not self._works:
            raise RuntimeError("CUDA error: no kernel image is available for execution on the device")
        return FakeTensor()


def test_pick_device():
    assert pick_device(FakeTorch(available=False)) == "cpu"
    assert pick_device(FakeTorch(capability=(8, 9))) == "cuda"  # RTX 40 系
    assert pick_device(FakeTorch(capability=(7, 5))) == "cuda"  # GTX 16 / RTX 20 系
    assert pick_device(FakeTorch(capability=(6, 1))) == "cpu"  # GTX 10 系：没有对应的内核
    assert pick_device(FakeTorch(capability=(8, 6), kernels_work=False)) == "cpu"  # 试算失败（驱动等问题）
