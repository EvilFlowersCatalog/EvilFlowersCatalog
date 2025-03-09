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
    """Creates a tar file compressed with xz."""

    def open_tarfile(self, file_path: str):
        return tarfile.open(file_path, "w:xz")

    def suffix(self) -> str:
        return ".xz"
