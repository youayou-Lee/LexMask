"""GLM Gen4 参赛者：与 HaS 同协议同调用路径，独立端点/模型。对齐参数同 has.py。"""

from __future__ import annotations

from .has import HasParticipant


class GlmGen4Participant(HasParticipant):
    name = "glm-gen4"
