from tidal.chain.retry import is_retryable_error


class TimeoutLikeError(Exception):
    pass


def test_retryable_timeout_message() -> None:
    assert is_retryable_error(TimeoutLikeError("request timeout")) is True


def test_non_retryable_revert_message() -> None:
    assert is_retryable_error(RuntimeError("execution reverted")) is False
