from pathlib import Path

from yuqing.services.source_tiers import SourceTierClassifier


def test_source_tier_table_distinguishes_authority_media_portal_and_unknown():
    classifier = SourceTierClassifier.from_yaml(
        Path(__file__).parents[2] / "yuqing" / "services" / "source_tiers.yaml"
    )

    assert classifier.classify("www.samr.gov.cn") == (1, "authority", True)
    assert classifier.classify("news.xinhuanet.com") == (2, "independent", True)
    assert classifier.classify("news.cctv.com") == (2, "independent", True)
    assert classifier.classify("news.cnr.cn") == (2, "independent", True)
    assert classifier.classify("finance.sina.com.cn") == (3, "syndicated", True)
    assert classifier.classify("unlisted.example") == (4, "unknown", False)
