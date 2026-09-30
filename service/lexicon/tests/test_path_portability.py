import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


class BenchmarkPathTests(unittest.TestCase):
    def setUp(self):
        self.script = (
            Path(__file__).resolve().parents[1] / "tools" / "benchmark_hotwords.py"
        )

    def load_benchmark(self):
        module_name = "voiceflypy_benchmark_hotwords_path_test"
        spec = importlib.util.spec_from_file_location(module_name, self.script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        fake_numpy = types.ModuleType("numpy")
        fake_sherpa = types.ModuleType("sherpa_onnx")
        with mock.patch.dict(
            sys.modules,
            {module_name: module, "numpy": fake_numpy, "sherpa_onnx": fake_sherpa},
        ):
            spec.loader.exec_module(module)
        return module

    def test_default_model_follows_source_root(self):
        benchmark = self.load_benchmark()
        expected = (
            self.script.resolve().parents[2]
            / "sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-"
            "int8-2026-06-05"
        )
        self.assertEqual(benchmark.DEFAULT_MODEL, expected)

    def test_source_has_no_developer_home_literal(self):
        source = self.script.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"/Users/[^/\"'\s]+")


if __name__ == "__main__":
    unittest.main()
