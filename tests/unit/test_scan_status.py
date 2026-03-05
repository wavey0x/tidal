from tidal.scanner.service import determine_scan_status


def test_status_success() -> None:
    assert determine_scan_status(pairs_seen=3, pairs_failed=0) == "SUCCESS"


def test_status_partial() -> None:
    assert determine_scan_status(pairs_seen=3, pairs_failed=1) == "PARTIAL_SUCCESS"


def test_status_failed_when_no_pairs_and_failures() -> None:
    assert determine_scan_status(pairs_seen=0, pairs_failed=2) == "FAILED"
