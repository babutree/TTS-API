# -*- coding: utf-8 -*-
"""默认部署不得把 WebSocket query key 交给 Uvicorn access log。"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class DeploymentLogContractTests(unittest.TestCase):
    def test_default_container_disables_uvicorn_access_log(self):
        """R24: 默认 Docker CMD 不记录含 ?key= 的原始 WebSocket URI。"""
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"--no-access-log"', dockerfile)

    def test_deployment_guides_keep_access_log_mitigation_visible(self):
        """运维文档必须说明镜像策略，避免把容器/代理原始日志误称为已脱敏。"""
        for filename in ("README.md", "README_CN.md"):
            with self.subTest(filename=filename):
                content = (ROOT / filename).read_text(encoding="utf-8")
                self.assertIn("--no-access-log", content)


if __name__ == "__main__":
    unittest.main()
