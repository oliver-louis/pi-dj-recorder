class RecorderError(RuntimeError):
    """Base error for recorder lifecycle failures."""


class AlreadyRecordingError(RecorderError):
    """Raised when a start request arrives while FFmpeg is active."""


class NotRecordingError(RecorderError):
    """Raised when a stop request arrives without an active recording."""


class DeviceUnavailableError(RecorderError):
    """Raised when FFmpeg cannot open the configured ALSA input."""


class WaveformGenerationError(RecorderError):
    """Raised when waveform extraction fails."""


class NotMeteringError(RecorderError):
    """Raised when metering stop is requested but no metering process is active."""


class TrackIdExportError(RecorderError):
    """Raised when track ID export cannot be generated from an on-air log."""
