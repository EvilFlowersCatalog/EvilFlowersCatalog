import tarfile


class CompressionStrategy:
    """Interface for compression strategy."""

    def open_tarfile(self, file_path: str):
        raise NotImplementedError()

    def suffix(self) -> str:
        return ""


class PlainCompressionStrategy(CompressionStrategy):
    """Creates an uncompressed tar file."""

    def open_tarfile(self, file_path: str):
        return tarfile.open(file_path, "w")


class XZCompressionStrategy(CompressionStrategy):
    """Creates a tar file compressed with xz.

    Preset 1, not tarfile's default 6: a catalog is mostly PDFs, which are compressed already.
    On sample catalog PDFs preset 6 ran at 3.7 MB/s for 77.0 % of the input, preset 1 at 10.6 MB/s
    for 77.5 %. At preset 6 the nightly backup spent ~17 min of a core on one 1.8 GB catalog
    (`lzma_code`, ~28 % of the dev pod's CPU in the eBPF profile of 2026-11-09).
    """

    PRESET = 1

    def open_tarfile(self, file_path: str):
        return tarfile.open(file_path, "w:xz", preset=self.PRESET)

    def suffix(self) -> str:
        return "xz"
