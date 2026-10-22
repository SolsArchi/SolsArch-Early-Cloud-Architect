"""Search and model adapters, with fake network functions so the tests run offline."""
import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
import facts
import web_tools
from test_pipeline import repo_in


class SearchAdapters(unittest.TestCase):
    def run_search(self, env, body, domains=("docs.aws.amazon.com",)):
        with mock.patch.dict(os.environ, env, clear=False):
            return web_tools.search("Amazon ECS", domains, provider=env_provider(env), fetcher=lambda url, payload=None, headers=None: body)

    def test_tavily_results_are_limited_to_the_vendor_domain(self):
        body = {"results": [{"url": "https://docs.aws.amazon.com/ecs/", "title": "ECS", "content": "x"}, {"url": "https://blog.example.com/ecs", "title": "b", "content": "y"}]}
        out = self.run_search({"TAVILY_API_KEY": "k"}, body)
        self.assertEqual([r["url"] for r in out], ["https://docs.aws.amazon.com/ecs/"])

    def test_brave_serper_and_google_shapes(self):
        cases = [({"BRAVE_API_KEY": "k"}, {"web": {"results": [{"url": "https://docs.aws.amazon.com/a", "title": "t", "description": "d"}]}}),
                 ({"SERPER_API_KEY": "k"}, {"organic": [{"link": "https://docs.aws.amazon.com/b", "title": "t", "snippet": "s"}]}),
                 ({"GOOGLE_CSE_KEY": "k", "GOOGLE_CSE_ID": "c"}, {"items": [{"link": "https://docs.aws.amazon.com/c", "title": "t", "snippet": "s"}]})]
        for env, body in cases:
            with self.subTest(env=list(env)):
                self.assertEqual(len(self.run_search(env, body)), 1)

    def test_http_pages_are_dropped(self):
        body = {"results": [{"url": "http://docs.aws.amazon.com/x", "title": "t", "content": "c"}]}
        self.assertEqual(self.run_search({"TAVILY_API_KEY": "k"}, body), [])

    def test_no_key_means_no_search(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(web_tools.search_provider())
            self.assertEqual(web_tools.search("x", ["a.com"]), [])


def env_provider(env):
    return {"TAVILY_API_KEY": "tavily", "BRAVE_API_KEY": "brave", "SERPER_API_KEY": "serper", "GOOGLE_CSE_KEY": "google"}[next(iter(env))]


class ModelAdapters(unittest.TestCase):
    def test_each_provider_shape_is_read(self):
        cases = [("anthropic", {"content": [{"text": "3"}]}), ("gemini", {"candidates": [{"content": {"parts": [{"text": "3"}]}}]}),
                 ("openai", {"choices": [{"message": {"content": "3"}}]})]
        for provider, body in cases:
            with self.subTest(provider=provider):
                self.assertEqual(web_tools.llm_complete("pick", provider=provider, fetcher=lambda url, payload=None, headers=None: body).strip(), "3")

    def test_no_model_means_empty_answer(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(web_tools.llm_complete("x"), "")

    def test_a_model_pick_outside_the_list_is_ignored(self):
        item = {"service": "Amazon ECS", "domains": ["docs.aws.amazon.com"], "query": "q", "key": "k"}
        text = "Amazon ECS is a fully managed container orchestration service that helps you run applications.\nAmazon ECS also integrates with other services for networking and logging."
        picked = web_tools.pick_sentence(item, text, facts.validate, llm=lambda prompt: "99")
        self.assertIn("Amazon ECS", picked)


class AutoVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_auto_records_a_valid_sentence_and_leaves_retired_pages_for_review(self):
        repo = repo_in(self.tmp, "shop", helpers.build_saas)
        helpers.pipeline(repo, "aws", "server")
        plan = facts.load_plan(repo)
        first = plan["verify_queue"][0]
        host = first["domains"][0]
        service = first["service"]
        pages = {}

        def fake_search(query, domains, **kw):
            return [{"url": "https://%s/page-%s" % (host, abs(hash(query)) % 1000), "title": "t", "snippet": "s"}]

        def fake_fetch(url):
            return "%s is a managed service that helps you run applications without managing servers." % service

        code, out = facts.cmd_auto(argparse.Namespace(repo=repo, limit=3, llm=False), search_fn=fake_search, fetch_fn=fake_fetch)
        self.assertEqual(code, 0)
        self.assertTrue(out["recorded"])
        saved = facts.load_facts(repo)["facts"]
        self.assertTrue(saved and all(f["verdict"] == "confirmed" for f in saved))

        retired = lambda url: "%s is deprecated and will no longer be available after next year." % service
        repo2 = repo_in(self.tmp, "shop2", helpers.build_saas)
        helpers.pipeline(repo2, "aws", "server")
        code, out = facts.cmd_auto(argparse.Namespace(repo=repo2, limit=3, llm=False), search_fn=fake_search, fetch_fn=retired)
        self.assertEqual(out["recorded"], [])
        self.assertTrue(out["needs_review"] or out["still_pending"])

    def test_without_a_search_api_it_says_so(self):
        repo = repo_in(self.tmp, "shop3", helpers.build_saas)
        helpers.pipeline(repo, "aws", "server")
        with mock.patch.dict(os.environ, {}, clear=True):
            code, out = facts.cmd_auto(argparse.Namespace(repo=repo, limit=3, llm=False))
        self.assertEqual(out["status"], "no_search")


if __name__ == "__main__":
    unittest.main()
