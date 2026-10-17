#!/usr/bin/env python3
"""ws-judge-core · FAKE_TEST 确定性测试 ASR Provider（公网评委 Demo 专用，测试条件）。

定位：
- Provider 名 = FAKE_TEST（**测试ASR模式**，非真实 ASR 模型、零网络、零付费调用）；
- 依据上传 WAV 内容 sha256 返回固定转录文本；转录文本与上游 4797485 内置
  FakeSemanticProvider 的确定性 fixtures 一一对应（sha256→固定转录→固定语义）；
- 未映射的 sha256 抛 ASRUnavailable('FAKE_TEST_UNMAPPED_AUDIO')：
  绝不猜测/伪造任意音频转录（未知音频=确定性失败，公网演示安全行为）；
- 诊断信息只含固定标识符，不含任何凭据/音频内容。

注入方式：通过上游官方注入点 create_app(settings, provider=None,
asr_provider=FakeTestASRProvider()) 使用，不修改 fitcore-core-real 任何文件。
"""

import hashlib
import time

from app.asr.base import ASRResult
from app.asr.real_provider import ASRUnavailable

PROVIDER_NAME = "FAKE_TEST"

# sha256 → 固定转录（与 WS-E 合成正弦音 WAV fixture manifest_real.json 一致；
# 转录文本与 4797485 FakeSemanticProvider fixtures 完全对应，测试条件声明）：
#   卧推60公斤8个        → SET_COMPLETED(60×8) COMMIT
#   8                    → BARE_NUMBER → pending
#   刚才第一组其实是50公斤 → CORRECTION(set1, weight 50)
#   下一组想试70公斤       → NEXT_SET_PLAN_CHANGED(70)
TRANSCRIPTS = {
    "3985f4fc3714d505c88a87be6581d727cb457b5f09a83614e8635447b9b4580c": "卧推60公斤8个",
    "245ec6030f7d94360884cf60f1eb4ad34ffb42bc567095095c29f92618c4aa3c": "8",
    "8ef7bd199e7c8323843e1aca57a0ee39c44bf23325d9662b7303df00ef6527f3": "刚才第一组其实是50公斤",
    "d49f71a7f3f43ed43acab329c8a0bd46c9445f426f3eae7790949e63791edb04": "下一组想试70公斤",
    "5196e41b963f7c6f039706684188006e0d5ef9e544701b53af084361297238c6": "8",
}


class FakeTestASRProvider:
    """FAKE_TEST：sha256 → 固定转录 + 固定语义（测试ASR模式，标注在诊断中）。"""

    provider_name = PROVIDER_NAME

    def __init__(self):
        self.diagnostics = {}
        self.calls = 0

    def transcribe_file(self, path):
        self.calls += 1
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        started = time.perf_counter()
        self.diagnostics = {
            "provider": PROVIDER_NAME,          # 测试ASR模式 显式标注
            "mode_label": "测试ASR模式",
            "transport": "deterministic_fake_test_fixture",  # 零网络
            "request_sent": False,
            "http_status": None,
            "schema_parse_success": False,
            "error_type": None,
            "test_condition": True,             # 显式测试条件标记
        }
        text = TRANSCRIPTS.get(digest)
        if text is None:
            self.diagnostics["error_type"] = "FAKE_TEST_UNMAPPED_AUDIO"
            self.diagnostics["sha256"] = digest
            raise ASRUnavailable("FAKE_TEST_UNMAPPED_AUDIO")
        self.diagnostics["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        self.diagnostics["sha256"] = digest
        return ASRResult(
            transcript=text,
            audio_duration_seconds=0.40,
            elapsed_ms=self.diagnostics["elapsed_ms"],
            http_status=200,
        )
