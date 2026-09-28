"""url_tags.meta_url_tags and MetaClient.create_ad_creative sending it. No network."""
import json
import unittest
from unittest.mock import patch

from url_tags import meta_url_tags


class UrlTags(unittest.TestCase):
    def test_all_three_meta_ids_as_unescaped_macros(self):
        tags = meta_url_tags()
        self.assertIn("fb_campaign={{campaign.id}}", tags)
        self.assertIn("fb_adset={{adset.id}}", tags)
        self.assertIn("fb_ad={{ad.id}}", tags)
        self.assertNotIn("%7B", tags, "braces must reach Meta unescaped")

    def test_a_param_the_destination_already_sets_is_not_repeated(self):
        tags = meta_url_tags("https://shop.example/?utm_source=newsletter&x=1")
        self.assertNotIn("utm_source=", tags)
        self.assertIn("utm_medium=paid", tags)

    def test_a_non_url_destination_still_gets_every_param(self):
        self.assertEqual(meta_url_tags("not a url"), meta_url_tags())

    def test_same_contract_as_reveal(self):
        self.assertEqual(
            meta_url_tags(),
            "utm_source=facebook&utm_medium=paid&utm_id={{campaign.id}}"
            "&fb_campaign={{campaign.id}}&fb_adset={{adset.id}}&fb_ad={{ad.id}}",
        )


class CreativeBody(unittest.TestCase):
    def _client(self):
        from meta_client import MetaClient
        c = MetaClient.__new__(MetaClient)
        c.account = "act_1"
        c.page_id = "page_1"
        return c

    def test_url_tags_sent_verbatim_when_given(self):
        c = self._client()
        with patch.object(type(c), "_post", autospec=True, return_value={"id": "cr1"}) as post:
            c.create_ad_creative(name="n", image_hash="h", link="https://x.example", message="m", headline="h", url_tags=meta_url_tags())
        body = post.call_args[0][2]
        self.assertEqual(body["url_tags"], meta_url_tags())
        self.assertIn("page_1", json.loads(body["object_story_spec"])["page_id"])

    def test_no_url_tags_key_when_none(self):
        c = self._client()
        with patch.object(type(c), "_post", autospec=True, return_value={"id": "cr1"}) as post:
            c.create_ad_creative(name="n", image_hash="h", link="https://x.example", message="m", headline="h")
        self.assertNotIn("url_tags", post.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
