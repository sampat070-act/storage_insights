from capacity_report import suggest_tier


def test_recent_bucket_stays_on_standard():
    assert suggest_tier(10) is None


def test_thirty_days_suggests_standard_ia():
    assert suggest_tier(30) == "S3 Standard-IA"

def test_deep_archive_for_very_old_bucket():
    assert suggest_tier(300) == "S3 Glacier Deep Archive"


def test_glacier_for_ninety_days():
    assert suggest_tier(90) == "S3 Glacier Flexible Retrieval"