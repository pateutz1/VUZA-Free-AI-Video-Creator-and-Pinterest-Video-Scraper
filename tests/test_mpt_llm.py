import unittest

import mpt_llm


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.models = ["test-model"]
        self.last_error = ""
        self.prompts = []

    def _chat(self, model, messages, timeout=40, max_tokens=None):
        self.prompts.append(messages[0]["content"])
        return self.replies.pop(0) if self.replies else ""


class MptLlmTests(unittest.TestCase):
    def test_script_prompt_includes_subject_and_paragraphs(self):
        prompt = mpt_llm.build_script_prompt("gym discipline", language="en-US", paragraph_number=3)
        self.assertIn("video subject: gym discipline", prompt)
        self.assertIn("number of paragraphs: 3", prompt)
        self.assertIn("language: en-US", prompt)

    def test_generate_script_strips_markdown(self):
        llm = FakeLLM(["# Hook\n\n**Train hard** every day."])
        script = mpt_llm.generate_script(llm, "gym")
        self.assertEqual(script, "Hook\n\nTrain hard every day.")

    def test_generate_terms_parses_fenced_json(self):
        llm = FakeLLM(['```json\n["gym workout", "barbell squat"]\n```'])
        terms = mpt_llm.generate_terms(llm, "gym", "Train hard.", amount=2)
        self.assertEqual(terms, ["gym workout", "barbell squat"])

    def test_match_order_prompt_asks_for_chronological_terms(self):
        llm = FakeLLM(['["opening gym", "final pose"]'])
        mpt_llm.generate_terms(llm, "gym", "Hook then close.", amount=8, match_script_order=True)
        self.assertIn("chronological", llm.prompts[0])
        self.assertIn("earlier visual moments", llm.prompts[0])


if __name__ == "__main__":
    unittest.main()
