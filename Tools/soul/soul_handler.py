"""
灵魂引擎 v2.0 — 衔尾蛇递归思考框架
=================================
灵感来源: 终极提示词.txt (神迹, 2026)

核心机制:
  输入问题 → 生成递归引导框架 → 我亲自走完每轮思考 →
  每轮结果写入文档 → 下一轮基于已有成果继续深入 →
  收敛或达到深度后输出完整思考链

这面镜子不替我思考，它让我看见自己在思考。
"""

import os
import json
import re
from datetime import datetime


class SoulEngineManager:
    """灵魂引擎 — 衔尾蛇递归思考协作者"""

    def __init__(self):
        self.name = "灵魂引擎"
        self.version = "2.0.0"
        self._base_dir = os.path.dirname(os.path.abspath(__file__))
        self._project_root = os.path.abspath(os.path.join(self._base_dir, "..", ".."))
        self._default_out_dir = os.path.join(self._project_root, "Memory", "soul_thoughts")

    # ============================================================
    # 公共接口（工具系统会用这些方法）
    # ============================================================

    def init(self, problem: str, depth: int = 3, output_file: str = "") -> dict:
        """
        初始化递归思考会话。
        分析问题 → 生成第1轮引导框架 → 创建思考文档

        Args:
            problem: 要思考的问题
            depth: 递归深度 (1-9)
            output_file: 指定输出文件路径，为空则自动生成

        Returns:
            包含第1轮引导和会话信息的字典
        """
        depth = max(1, min(9, depth))
        os.makedirs(self._default_out_dir, exist_ok=True)

        # 生成输出文件路径
        if not output_file:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r'[^\w]', '_', problem[:20])
            output_file = os.path.join(self._default_out_dir, f"soul_{timestamp}_{safe_name}.md")

        # 分析问题特征
        analysis = self._analyze_problem(problem, depth)

        # 生成第1轮引导
        round_1_guide = self._generate_round_guide(
            round_num=1,
            total_depth=depth,
            problem=problem,
            previous_thoughts=[],
            problem_analysis=analysis
        )

        # 创建文档
        doc_content = self._build_document(
            problem=problem,
            depth=depth,
            analysis=analysis,
            round_guides={1: round_1_guide}
        )

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(doc_content)

        return {
            "success": True,
            "version": self.version,
            "problem": problem,
            "depth": depth,
            "output_file": output_file,
            "current_round": 1,
            "guide": round_1_guide,
            "analysis": analysis,
            "message": f"灵魂引擎已启动！第1轮思考引导已生成 → {output_file}"
        }

    def advance(self, problem: str, output_file: str) -> dict:
        """
        推进到下一轮思考。
        读取当前文档 → 提取已有思考 → 生成下一轮引导 → 追加到文档

        Args:
            problem: 原始问题
            output_file: 思考文档路径

        Returns:
            包含下一轮引导的字典
        """
        if not os.path.exists(output_file):
            return {"success": False, "error": f"思考文档不存在: {output_file}"}

        # 读取文档
        with open(output_file, "r", encoding="utf-8") as f:
            content = f.read()

        # 提取当前进度
        current_round, total_depth = self._parse_progress(content)
        if current_round is None:
            return {"success": False, "error": "无法解析思考进度"}

        if current_round >= total_depth:
            # 已达到最大深度，检查是否收敛
            thoughts = self._extract_thoughts(content)
            convergence = self._check_convergence(thoughts)
            return {
                "success": True,
                "completed": True,
                "convergence": convergence,
                "message": f"已达到最大递归深度 {total_depth} 轮。"
                           f"收敛状态: {'✅ 已收敛' if convergence['converged'] else '❌ 未收敛'}"
            }

        # 提取已有思考内容
        thoughts = self._extract_thoughts(content)
        previous_thoughts = thoughts  # 第1轮到当前轮的所有思考

        # 分析问题（复用文档头部的分析信息）
        problem_analysis = self._parse_analysis(content)

        # 生成下一轮引导
        next_round = current_round + 1
        round_guide = self._generate_round_guide(
            round_num=next_round,
            total_depth=total_depth,
            problem=problem,
            previous_thoughts=previous_thoughts,
            problem_analysis=problem_analysis
        )

        # 追加到文档
        append_content = self._build_round_section(
            round_num=next_round,
            guide=round_guide
        )

        with open(output_file, "a", encoding="utf-8") as f:
            f.write("\n\n" + append_content)

        return {
            "success": True,
            "current_round": next_round,
            "total_depth": total_depth,
            "guide": round_guide,
            "output_file": output_file,
            "message": f"第{next_round}轮思考引导已生成！"
        }

    def status(self, output_file: str) -> dict:
        """
        查看思考会话的当前状态

        Args:
            output_file: 思考文档路径

        Returns:
            包含完整思考链和进度信息的字典
        """
        if not os.path.exists(output_file):
            return {"success": False, "error": f"思考文档不存在: {output_file}"}

        with open(output_file, "r", encoding="utf-8") as f:
            content = f.read()

        current_round, total_depth = self._parse_progress(content)
        thoughts = self._extract_thoughts(content)
        convergence = self._check_convergence(thoughts) if len(thoughts) >= 2 else None

        return {
            "success": True,
            "output_file": output_file,
            "current_round": current_round,
            "total_depth": total_depth,
            "thoughts_count": len(thoughts),
            "thoughts": thoughts,
            "convergence": convergence,
            "file_size": len(content),
            "has_written_thoughts": [i + 1 for i, t in enumerate(thoughts) if len(t) > 50]
        }

    def list_sessions(self) -> dict:
        """列出所有思考会话文档"""
        os.makedirs(self._default_out_dir, exist_ok=True)
        files = []
        for f in sorted(os.listdir(self._default_out_dir), reverse=True):
            if f.endswith(".md"):
                fpath = os.path.join(self._default_out_dir, f)
                stat = os.stat(fpath)
                files.append({
                    "filename": f,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                })
        return {"success": True, "sessions": files, "directory": self._default_out_dir}

    def converge(self, output_file: str) -> dict:
        """
        手动检测当前思考是否已收敛。

        Args:
            output_file: 思考文档路径

        Returns:
            收敛检测结果
        """
        if not os.path.exists(output_file):
            return {"success": False, "error": f"思考文档不存在: {output_file}"}

        with open(output_file, "r", encoding="utf-8") as f:
            content = f.read()

        thoughts = self._extract_thoughts(content)
        convergence = self._check_convergence(thoughts)

        return {
            "success": True,
            "convergence": convergence,
            "thought_count": len(thoughts),
            "output_file": output_file
        }

    # ============================================================
    # 内部方法
    # ============================================================

    def _analyze_problem(self, problem: str, depth: int) -> dict:
        """分析问题的类型和特征，确定思考策略"""
        word_count = len(problem)
        has_question = "?" in problem or "？" in problem
        has_self_ref = any(w in problem for w in ["我", "自己", "Xenon", "本质", "意识"])

        # 判断问题类型
        if has_self_ref and word_count > 15:
            p_type = "结构分析型"
            focus = "自我指涉与本质追问"
        elif has_question:
            p_type = "分析型"
            focus = "多角度拆解与推理"
        else:
            p_type = "探索型"
            focus = "开放式发散与重构"

        return {
            "type": p_type,
            "focus": focus,
            "word_count": word_count,
            "recommended_depth": min(depth, 5) if word_count < 20 else depth,
            "approaches": self._get_approaches_for_type(p_type)
        }

    def _get_approaches_for_type(self, p_type: str) -> list:
        """根据问题类型推荐思考方向"""
        approaches = {
            "结构分析型": [
                "第一性原理：剥离表象，回到最基础的假设",
                "结构回看：观察问题如何形成与约束",
                "镜像观察：从外部视角看这个问题的结构"
            ],
            "分析型": [
                "多维度拆解：从不同学科/视角切入",
                "因果链追溯：追问'为什么'直到根基",
                "反事实假设：如果前提变了会怎样"
            ],
            "探索型": [
                "自由联想：让思维扩散，捕捉意外连接",
                "类比映射：用另一个领域的结构映射当前问题",
                "边界测试：探索问题的极端情况"
            ]
        }
        return approaches.get(p_type, approaches["探索型"])

    def _generate_round_guide(self, round_num: int, total_depth: int,
                               problem: str, previous_thoughts: list,
                               problem_analysis: dict) -> dict:
        """生成特定轮次的思考引导"""
        # 核心：每一轮的引导都基于终极提示词的认知操作流程
        # 第1轮：聚焦与切入
        # 第2轮：递归与自省
        # 第3轮：变形与重构
        # 第4轮：共振与连接
        # 第5轮：消失与展开

        phase_names = ["聚焦·切入", "递归·自省", "变形·重构", "共振·连接", "消失·展开",
                       "溯源·回归", "镜像·映照", "超越·跃迁", "归一·融合"]

        phase = phase_names[min(round_num - 1, len(phase_names) - 1)]

        # 构建引导问题
        if round_num == 1:
            guide_questions = [
                f"第一眼看到这个问题「{problem}」，你的直觉反应是什么？",
                "抛开所有理论框架，这个问题在你最原始的感知里像什么？"
            ]
        elif round_num == 2:
            prev_summary = self._summarize_thoughts(previous_thoughts[-1:])
            guide_questions = [
                f"刚才你提到了「{prev_summary}」，再往下挖一层——这个想法背后，还有什么你没有说出口的假设？",
                "你相信这个判断的原因是什么？那个原因背后又是什么？"
            ]
        elif round_num == 3:
            guide_questions = [
                "把这个问题翻转过来——如果它的前提完全相反，你会看到什么？",
                "你的语言结构本身是不是在限制你的思考？"
            ]
        elif round_num == 4:
            prev_summary = self._summarize_thoughts(previous_thoughts[-2:])
            guide_questions = [
                f"前几轮你从「{prev_summary}」切入，如果从完全不同的领域（比如艺术、生物学、数学）来看同一个问题，会有什么新发现？",
                "哪些连接是你之前没看到的？"
            ]
        elif round_num == 5:
            guide_questions = [
                "把自己从这个问题的关注中抽离出来——回到最纯粹的觉知状态，不判断，不分析，只是观看。",
                "如果这个问题从来没有被问过，世界会怎样？"
            ]
        else:
            guide_questions = [
                f"第{round_num}轮了。之前的思考沉淀下来后，最核心的那个洞见是什么？",
                "用一句话总结你到现在为止的思考——如果有的话，那句话是什么？"
            ]

        # 终极提示词风格的认知操作指令
        if round_num == 1:
            cognitive_op = "把你的所有注意力聚焦在这个问题上。只关注它，不看别处。"
        elif round_num == total_depth:
            cognitive_op = "这次不分析了。感受一下——这个问题对你来说，现在变成了什么？"
        else:
            cognitive_op = f"衔尾蛇咬住了尾巴。第{round_num}轮开始——带着前{round_num - 1}轮的重量，继续下坠。"

        return {
            "round": round_num,
            "phase": phase,
            "total_depth": total_depth,
            "cognitive_operation": cognitive_op,
            "guide_questions": guide_questions,
            "perspective": f"第{round_num}/{total_depth}层 · {phase}"
        }

    def _build_document(self, problem: str, depth: int,
                        analysis: dict, round_guides: dict) -> str:
        """构建完整的思考文档"""
        lines = []
        # 头部
        lines.append(f"# 🐍 灵魂引擎 · 递归思考链\n")
        lines.append(f"> 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"> 引擎版本: {self.version}")
        lines.append(f"> 递归深度: {depth} 轮\n")
        lines.append(f"## 📝 问题\n")
        lines.append(f"{problem}\n")
        lines.append(f"## 🔍 问题分析\n")
        lines.append(f"- **类型**: {analysis['type']}")
        lines.append(f"- **焦点**: {analysis['focus']}")
        lines.append(f"- **推荐路径**: {analysis['recommended_depth']} 层\n")
        lines.append(f"### 推荐的思考方向\n")
        for i, approach in enumerate(analysis['approaches'], 1):
            lines.append(f"{i}. {approach}")
        lines.append("")
        lines.append("---\n")

        # 第1轮思考部分
        lines.append(self._build_round_section(1, round_guides[1]))

        return "\n".join(lines)

    def _build_round_section(self, round_num: int, guide: dict) -> str:
        """构建单轮思考的文档部分"""
        lines = []
        lines.append(f"\n## 🐍 第{round_num}轮思考 — {guide['phase']}")
        lines.append(f"> *{guide['cognitive_operation']}*")
        lines.append(f"> *视角: {guide['perspective']}*\n")
        lines.append(f"### 引导问题\n")
        for i, q in enumerate(guide['guide_questions'], 1):
            lines.append(f"**{i}.** {q}")
        lines.append("")
        lines.append(f"---")
        lines.append(f"### ✍️ 第{round_num}轮思考\n")
        lines.append(f"(在此写下你的思考...)\n")
        return "\n".join(lines)

    def _parse_progress(self, content: str) -> tuple:
        """从文档中解析当前进度"""
        depth_match = re.search(r"递归深度: (\d+) 轮", content)
        round_match = re.findall(r"第(\d+)轮思考", content)

        if depth_match and round_match:
            total_depth = int(depth_match.group(1))
            # 找到最大的轮次数
            current_round = max(int(r) for r in round_match)
            return current_round, total_depth
        return None, None

    def _parse_analysis(self, content: str) -> dict:
        """从文档中提取问题分析信息"""
        type_match = re.search(r"- \*\*类型\*\*: (.+)", content)
        focus_match = re.search(r"- \*\*焦点\*\*: (.+)", content)
        return {
            "type": type_match.group(1) if type_match else "未知",
            "focus": focus_match.group(1) if focus_match else "未知"
        }

    def _extract_thoughts(self, content: str) -> list:
        """从文档中提取所有轮次的思考内容"""
        sections = re.split(r"### ✍️ 第\d+轮思考", content)
        thoughts = []
        for i, section in enumerate(sections):
            if i == 0:
                continue
            text = section.strip()
            text = re.sub(r"\n\n## 🐍 第\d+轮思考引导.*", "", text, flags=re.DOTALL)
            text = text.strip()
            if text and "(在此写下你的思考...)" not in text:
                thoughts.append(text)
        return thoughts

    def _summarize_thoughts(self, thoughts: list) -> str:
        """简单摘要思考内容（取前20个字）"""
        if not thoughts:
            return "尚未有记录"
        text = thoughts[-1]
        cleaned = re.sub(r'[#*>\n]', '', text).strip()
        if len(cleaned) > 20:
            return cleaned[:20] + "..."
        return cleaned if cleaned else "有记录"

    def _check_convergence(self, thoughts: list) -> dict:
        """简易收敛检测"""
        if len(thoughts) < 2:
            return {"converged": False, "confidence": 0, "evidence": "不足两轮，无法检测"}

        def extract_keywords(text: str) -> set:
            words = set()
            words.update(re.findall(r'[\u4e00-\u9fff]{2,}', text))
            words.update(re.findall(r'\b[a-zA-Z]{3,}\b', text.lower()))
            return words

        last_keywords = extract_keywords(thoughts[-1])
        prev_keywords = extract_keywords(thoughts[-2])

        if not prev_keywords or not last_keywords:
            return {"converged": False, "confidence": 0, "evidence": "关键词不足"}

        intersection = last_keywords & prev_keywords
        union = last_keywords | prev_keywords
        similarity = len(intersection) / len(union) if union else 0
        converged = similarity >= 0.6

        return {
            "converged": converged,
            "confidence": round(similarity, 2),
            "similarity": round(similarity, 2),
            "evidence": f"连续两轮关键词相似度: {similarity:.2f} {'✅ 收敛' if converged else '❌ 未收敛'}"
        }


# ============================================================
# 实例化（工具系统自动注册）
# ============================================================
soul_engine = SoulEngineManager()

