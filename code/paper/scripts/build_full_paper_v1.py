"""Build an editable review manuscript from the official template and frozen tables.

This script prepares a review DOCX only. It never invokes an official evaluator,
changes a solver, or exports the submission PDF.
"""

from __future__ import annotations

import csv
import io
import json
import re
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from generate_problem1_chapter import (
    add_caption,
    add_heading,
    add_paragraph,
    add_table,
    east_asian_font,
)


ROOT = Path(__file__).resolve().parents[3]
PAPER = ROOT / "code" / "paper"
FIG = ROOT / "figures"
TEMPLATE = PAPER / "templates" / "官方模板完整转换.docx"
Q1_DRAFT = PAPER / "draft" / "问题一章节草稿.docx"
OUT = PAPER / "draft" / "2026华为杯A题完整论文V1-审阅.docx"
TITLE = "面向 NPU 多核与共享 L2 的计算图切分与调度"


def rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def block_text(element) -> str:
    return "".join(element.xpath(".//w:t/text()"))


def image(doc, path: Path, caption: str, *, width=15.5) -> None:
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(6)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.keep_with_next = True
    paragraph.add_run().add_picture(str(path), width=Cm(width))
    add_caption(doc, caption)


def chinese_table_header(table, *, first_column=False) -> None:
    """Keep Chinese table labels at the official 12 pt, even in numeric appendices."""
    for cell in table.rows[0].cells:
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                east_asian_font(run, "宋体", 12, True)
    if first_column:
        for row in table.rows[1:]:
            for paragraph in row.cells[0].paragraphs:
                for run in paragraph.runs:
                    east_asian_font(run, "宋体", 12)


def compact_numeric_table(table) -> None:
    """Tighten only the numeric appendix rows; Chinese header stays 12 pt."""
    for row in table.rows[1:]:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.line_spacing = 1.0


def copy_problem1_body(doc, source) -> None:
    """Copy Q1 main chapter, retaining native equations, tables, and image parts."""
    body = list(source.element.body)
    assert block_text(body[0]).startswith("4 问题一")
    assert block_text(body[43]).startswith("附录 A")
    assert body[42].xpath(".//w:br")
    image_ids: dict[str, str] = {}
    for index, item in enumerate(body[:42]):
        copied = deepcopy(item)
        if index == 39:
            # The tall 36-group comparison otherwise leaves most of the
            # preceding page empty. Scale it slightly in the integrated paper.
            for extent in copied.xpath(".//wp:extent") + copied.xpath(".//a:ext"):
                if extent.get("cx") and extent.get("cy"):
                    extent.set("cx", str(round(int(extent.get("cx")) * 11.0 / 12.2)))
                    extent.set("cy", str(round(int(extent.get("cy")) * 11.0 / 12.2)))
        for blip in copied.xpath(".//a:blip"):
            old = blip.get(qn("r:embed"))
            if old:
                if old not in image_ids:
                    part = source.part.related_parts[old]
                    image_ids[old], _ = doc.part.get_or_add_image(io.BytesIO(part.blob))
                blip.set(qn("r:embed"), image_ids[old])
        for text_node in copied.xpath(".//w:t"):
            value = text_node.text or ""
            value = re.sub(r"图([1-3])[—-]([1-3])",
                           lambda m: f"图{int(m.group(1))+1}—{int(m.group(2))+1}", value)
            value = re.sub(r"图([1-3])", lambda m: f"图{int(m.group(1))+1}", value)
            text_node.text = value
        text_nodes = copied.xpath(".//w:t")
        if index == 3 and text_nodes:
            text_nodes[-1].text = (text_nodes[-1].text or "") + "本节主要符号见表1。"
        if index == 38 and text_nodes:
            text_nodes[0].text = "表3显示，" + (text_nodes[0].text or "")
        doc.element.body.insert(len(doc.element.body) - 1, copied)


def copy_problem1_appendix(doc, source) -> None:
    for item in list(source.element.body)[43:50]:
        copied = deepcopy(item)
        for text_node in copied.xpath(".//w:t"):
            value = text_node.text or ""
            value = value.replace("图1—3", "图2—4")
            text_node.text = value
        doc.element.body.insert(len(doc.element.body) - 1, copied)


def make_cover(doc) -> None:
    # Preserve the official four-logo title block and the cover table. The
    # trailing empty template paragraphs would create a blank second page.
    body = doc.element.body
    assert len(doc.tables) == 1
    assert block_text(body[2]).startswith("中国研究生")
    assert block_text(body[14]).startswith("中国研究生")
    for item in list(body)[7:-1]:
        body.remove(item)
    # The official DOC stores one logo twice: an embedded copy plus an old
    # absolute-path link from its author. Keep the embedded image and remove
    # only the dangling external href/relationship.
    for image_data in body.iter("{urn:schemas-microsoft-com:vml}imagedata"):
        href = image_data.get(qn("r:href"))
        if href and image_data.get(qn("r:id")) and href in doc.part.rels:
            relation = doc.part.rels[href]
            if relation.is_external and relation.reltype.endswith("/image"):
                image_data.attrib.pop(qn("r:href"), None)
                del doc.part.rels[href]
    school = doc.tables[0].cell(0, 1)
    school.text = "西南民族大"
    for p in school.paragraphs:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in p.runs:
            east_asian_font(run, "宋体", 16, True)
    # Do not leak the template author's personal metadata into an anonymous
    # competition manuscript.
    doc.core_properties.author = ""
    doc.core_properties.last_modified_by = ""
    doc.core_properties.title = TITLE
    doc.core_properties.subject = ""
    doc.core_properties.comments = ""
    for paragraph in doc.sections[0].first_page_footer.paragraphs:
        paragraph.clear()
    # Other cover fields are intentionally blank pending team verification.
    doc.add_page_break()


def front_matter(doc) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(16)
    east_asian_font(p.add_run(TITLE), "黑体", 16, True)
    add_heading(doc, "摘 要", 1)
    add_paragraph(
        doc,
        "给定具有算子依赖、张量存储和双流水线工作量的计算图，"
        "多核调度不仅要分摊计算，还要避免切图造成的同步等待和数据搬运。"
        "本文在原版官方评估器下分别处理跨子图数据经 DDR 周转的场景 A、"
        "含共享 DDR 与核内存储约束的场景 B，以及加入共享只读 L2 的场景。"
        "先按依赖层组织合法子图，以弱连通分支和边界张量构造多粒度切图候选；"
        "再在固定切图上考察场景 B 专用核映射，并以低核方案继承和阶段一实际赢家作回退。"
        "对于 L2，固定同一批问题二阶段一方案，分别在有、无 L2 条件下评估，"
        "再单独衡量 L2 专用顺序与映射调整的收益。"
    )
    add_paragraph(
        doc,
        "100 个案例、1—5 核的官方结果显示：场景 A 的五核逐例加速比算术均值为 2.8905；"
        "场景 B 最终组合为 3.2347，在 400 个多核案例—核数组合中，"
        "124 组比阶段一实际赢家更快、没有退化组。"
        "问题三在五核下，同一最终方案的有/无 L2 完工时间比均值为 1.0248；"
        "在有 L2 的同一硬件下，最终方案相对冻结方案的完工时间比均值为 1.0121。"
        "这些收益并非对每张图都成立：输入重复读入、共享 DDR 排队、Spill "
        "和 FIFO 淘汰会改变关键路径。故本文将近似模型限定为候选筛选工具，"
        "所有最终选择和结论均依据官方模拟器的逐例结果。"
    )
    p = add_paragraph(doc, "关键词：计算图切分；多核调度；共享 DDR；L2 缓存；官方仿真", indent=False)
    p.paragraph_format.space_before = Pt(10)
    doc.add_page_break()


def introduction(doc) -> None:
    add_heading(doc, "1 问题背景与研究思路", 1)
    add_paragraph(
        doc,
        "神经网络处理器把计算划分给多个核心时，算子之间的依赖不会随核心数增加而消失。"
        "一条跨核依赖可能引出同步、张量写回和再次读入；与此同时，多个核心共用 DDR，"
        "单核的片上容量也限制了中间结果的驻留。只看各核工作量是否均衡，"
        "容易把等待和搬运留在关键路径上。本题要求在同一批计算图上依次回答切图、"
        "场景 B 调度以及共享 L2 带来的变化，适合用同一套可追溯方案接口，"
        "但不能把三种官方评分口径混为一谈。"
    )
    add_paragraph(
        doc,
        "本文的做法是先形成少量结构差异明确的合法切图，再让官方仿真决定候选去留。"
        "上行秩与最早完成时间借鉴经典 HEFT 调度框架[1]，"
        "本文只在子图粒度和代价口径上作适配，不把它作为独立新算法。"
        "问题一用完整依赖层、弱连通分支与边界张量控制子图边界；"
        "问题二保留这些切图作为初始化，只在固定子图集合上有界调整核映射，"
        "并始终允许阶段一实际赢家回退；问题三不沿用问题二最终组合，"
        "而是固定较早冻结的阶段一方案，以同方案的有/无 L2 配对拆开硬件效应，"
        "再衡量针对 L2 的方案优化。图1给出这三条证据链的关系。"
    )
    image(doc, FIG / "flow_overall_model.png",
          "图1 三问求解与评估关系；问题三从冻结的场景 B 阶段一方案出发", width=14.5)
    add_heading(doc, "2 题目分析与任务边界", 1)
    add_paragraph(
        doc,
        "原始输入是一张算子—张量有向无环图 G₀[2]。按照题目和官方接口处理原始搬运算子后，"
        "得到可切分的计算图 G=(V,E)。方案用算子到子图的映射及各核心的子图执行序列表示；"
        "每个算子只属于一个子图，每个子图只在一个核心执行，依赖和核内顺序都必须合法。"
        "若请求 k 个核心却只使用其中一部分，其余核心以官方允许的零操作空 Task 表示。"
    )
    add_paragraph(
        doc,
        "问题一的场景 A 着重考察切图和多核完成时间；问题二的场景 B 在此基础上显式引入"
        "共享 DDR、核心内存、Spill、输入重复读取与跨核直接依赖。"
        "问题三再加入容量 1 MiB、读速率 250 byte/cycle 的共享只读 L2；"
        "DDR 读写仍受 60 byte/cycle 的共享带宽约束。"
        "因此后两问的通信与存储代价必须按各自官方源码解释，不能把场景 A 的代理参数当作场景 B 实测。"
    )
    add_heading(doc, "3 合法性、评价指标与实验设计", 1)
    add_paragraph(
        doc,
        "首要指标是官方评估器给出的总完成时间 Makespan，单位为 cycle。"
        "同一案例在 k 核的逐例加速比为该问同一口径的单核官方时间除以 k 核官方时间；"
        "下文报告的平均加速比先逐例求比，再对 100 例取算术平均。"
        "新增搬运以 byte 计；场景 B 另记录 Spill、跨核有效载荷、传输次数和每核利用率。"
        "候选之间先比 Makespan；相同才比新增搬运。这个字典序是本文冻结的选择规则，"
        "不是题目另行规定的官方综合分数。"
    )
    add_paragraph(
        doc,
        "实验覆盖 100 张图及 1—5 核共 500 个案例—核数组合。"
        "问题二与问题三的开发、验证和留出案例在使用收益调规则之前冻结；"
        "留出结果仅用于最终报告。每个候选记录合法性、官方结果及其输入、配置、方案和实现身份，"
        "失败候选单独保留，不以只展示赢家代替实验记录。"
        "不同候选的估计时间不直接比较为最终结论；文中所有性能主张均可由附录和随附 CSV 复算。"
    )
    add_paragraph(
        doc,
        "数据分析辅助披露（审阅待核）：CSV 汇总、配对核算与图表整理曾使用 AI 辅助；"
        "原始性能数值均来自官方评估器，论文数值按原表复算。"
        "所用工具的名称、型号、开发机构及版本发布日期，须由参赛队核实后"
        "在此处补齐，不能以“AI 辅助”四字代替正式披露。"
    )


def problem2(doc, data: list[dict[str, str]], aggregate: list[dict[str, str]]) -> None:
    add_heading(doc, "5 问题二：场景 B 下的核映射与方案回退", 1)
    add_heading(doc, "5.1 从子图商图到场景 B 代价", 2)
    add_paragraph(
        doc,
        "问题二沿用问题一生成的单子图、B0、B1 和 B2A 三种窗口共六类切图，"
        "但不沿用场景 A 的评分。固定一类切图时，决策是每个子图的目标核心及其合法执行序列。"
        "同一输入张量在每个消费核心只计一次读入；普通跨核张量按"
        "（张量、源核心、目标核心）去重，而直接算子依赖按官方实现逐边插入。"
        "输出写回、单核数据驻留、容量不足引出的 Spill 与共享 DDR 排队同时作用于时间线。"
        "所以“跨核字节少”或“Spill 为零”均不能单独保证 Makespan 最小。"
    )
    add_paragraph(
        doc,
        "候选筛选器使用子图就绪时刻、各核 M/V Pipe 累计负载、预计跨核同步、"
        "去重输入读取、共享 DDR 工作量和片上容量超额等信息构造确定性代理。"
        "这些量仅用来排序搜索方向，不保证是官方 Makespan 的上下界。"
        "官方评估器会根据实际事件排队决定关键路径；例如固定 500 cycle 的同步延迟"
        "只作用于具体跨核依赖，不能乘上传输总数充当完工时间。"
    )
    add_heading(doc, "5.2 有界局部映射与候选组合", 2)
    add_paragraph(
        doc,
        "对五类非 single 切图，先保留原映射，再生成唯一冻结策略 map_locality 的新映射。"
        "该策略在子图拓扑就绪顺序中试探各核心，以预计完成时刻为首要比较量，"
        "并依次考虑跨核有效载荷、传输数、输入重复读取和 Pipe 负载；"
        "随后仅在至多 12 个高优先级边界子图上进行最多两步单子图迁核。"
        "切图边界在本次最终方法中不改变，因此不能把这一步称为“联合切图优化”。"
        "新方案必须重建官方接口并通过完整合法性检查。"
    )
    add_paragraph(
        doc,
        "每个多核组合至少比较 single、五个原映射和五个场景 B 映射，"
        "再加入已验证的低核赢家空核继承。"
        "阶段一该 case-core 的实际官方赢家始终是全局回退；"
        "仅当新候选的官方（Makespan，新增搬运）字典序更优时才替换。"
        "方案、输入图、配置、官方评估器与实现版本参与缓存身份，"
        "跨环境记录必须重新核对哈希，不能只凭旧绝对路径命中。"
        "核内排序 Gate A 在三组预注册探针中没有带来最终 case-core 改善，"
        "故本稿停止该排序分支的扩大实验，但不据此断言所有排序策略无效。"
    )
    add_heading(doc, "5.3 全量结果与严格配对比较", 2)
    assert len(data) == 500 and len(aggregate) == 5
    assert all(row["status"] == "success" for row in data)
    assert sum(int(r["improved_vs_stage1"] == "True") for r in data) in (124, 133)
    tab = []
    for r in sorted(aggregate, key=lambda x: int(x["cores"])):
        tab.append([
            r["cores"],
            f"{float(r['mean_speedup']):.4f}",
            f"{float(r['median_speedup']):.4f}",
            f"{float(r['mean_added_copy_bytes'])/2**20:.2f}",
            f"{float(r['mean_spill_added_copy_bytes'])/2**20:.2f}",
            f"{float(r['mean_requested_core_utilization']):.3f}",
        ])
    chinese_table_header(add_table(
        doc, ["核数", "平均加速比", "中位加速比", "平均新增搬运/MiB",
              "平均 Spill/MiB", "平均请求核利用率"],
        tab, "表4 场景 B 最终方案的 100 例官方汇总", font_size=10))
    image(doc, FIG / "q2" / "result_q2_scaling.png",
          "图5 场景 B 的 1—5 核逐例加速比；分母为同案例 single 的官方时间")
    add_paragraph(
        doc,
        "表4和图5表明，1—5 核的平均逐例加速比分别为 "
        "1.0000、1.7550、2.3568、2.8674 和 3.2347。"
        "这些数字来自 500 个最终 case-core，不能由场景 A 的结果推算。"
        "多核 400 组中，124 组相对阶段一实际赢家缩短 Makespan，"
        "另有 9 组在 Makespan 相同的情况下减少新增搬运；其余 267 组完全持平。"
        "没有最终退化组是回退机制的结果，并不表示任意单个新映射普遍优于原方案。"
    )
    chinese_table_header(add_table(
        doc, ["集合", "案例数", "多核组数", "更快组数", "更快案例数", "平均相对改善"],
        [["开发", "12", "48", "14", "7", "0.6247%"],
         ["验证", "18", "72", "20", "14", "0.4547%"],
         ["留出", "70", "280", "90", "49", "0.6352%"]],
        "表5 问题二按冻结分层的阶段一赢家配对比较", font_size=10),
        first_column=True)
    image(doc, FIG / "q2" / "result_q2_holdout_gain.png",
          "图6 问题二 70 个留出案例的多核改善；每核 70 组，零收益组单独保留")
    add_paragraph(
        doc,
        "表5和图6显示，留出集的 280 个多核组合中，90 组严格更快，涉及 49 个案例；"
        "其余组合由原方案回退或与其打平。平均改善约 0.6352%，"
        "但逐组改善的中位数为零，说明收益集中于部分计算图。"
        "图6按核数展示这种不均匀性；不能把 70 例留出结果外推为任意工作负载的保证。"
    )
    image(doc, FIG / "q2" / "result_q2_copy_spill_tradeoff.png",
          "图7 场景 B 中新增搬运、Spill 与完工时间的权衡；同一 case-core 配对")
    add_paragraph(
        doc,
        "图7进一步表明，减少 Spill 或跨核有效载荷并不必然缩短时间。"
        "共享 DDR 中的输入重复读、输出写回和跨核依赖可能改变排队顺序。"
        "因此本文只把这些量用作诊断，最终保留官方 Makespan 最短的合法方案。"
        "阶段一全局回退保留了稀疏但稳定的改进，也暴露了当前固定切图搜索的边界："
        "高 Spill 若主要由子图边界决定，单独迁核难以消除；"
        "本次未在最终留出集上追加未预注册的切图修补。"
    )


def problem3(doc, data: list[dict[str, str]], curve: list[dict[str, str]]) -> None:
    add_heading(doc, "6 问题三：共享只读 L2 的配对评估与方案调整", 1)
    add_heading(doc, "6.1 同方案硬件效应与 2×2 设计", 2)
    add_paragraph(
        doc,
        "问题三使用问题二 Stage 1 已冻结的 500 个实际赢家方案作为 G1 起点，"
        "不把问题二后来选出的 map_locality 方案混入该实验。"
        "对每个 case-core 保持切图、核映射与核内顺序完全相同，"
        "分别由官方评估器计算无 L2 与有 L2 的完工时间。"
        "两者之比回答“只增加这块硬件会怎样”；若同时换了方案，就无法归因。"
        "L2 为共享只读、容量 1 MiB，读速率 250 byte/cycle；"
        "官方实现还规定发起时查询、搬运完成时填充和 FIFO 淘汰。"
    )
    add_paragraph(
        doc,
        "第二条比较在同一有 L2 硬件下，把冻结方案与针对 L2 调整后的最终方案配对，"
        "回答“方案优化又带来多少”。"
        "为检验优化是否只在 L2 条件下有效，最终报告还让最终方案在无 L2 条件下复评，"
        "形成冻结/最终方案 × 无/有 L2 的 2×2 表。"
        "四个完工时间来自同一张图、同一核数，"
        "不把两种不同分母的加速比混乘成未经定义的总收益。"
    )
    add_heading(doc, "6.2 L2 复用窗口顺序与共享输入映射", 2)
    add_paragraph(
        doc,
        "G2 先固定冻结切图和核映射，只改变合法核内子图顺序。"
        "对于每个就绪子图，估计输入在发起时是否命中、"
        "DDR 与 L2 两条服务队列的预计结束时刻、"
        "跨核前驱释放时间和片上驻留压力。"
        "两种确定性排序分别偏向短期复用窗口与关键路径平衡，"
        "每次都重建官方方案并检查依赖与核心顺序。"
        "在排序赢家上，再尝试最多两个有共享输入机会的单子图迁核候选；"
        "该映射分支与排序分支分开记录，不能把两者收益重复相加。"
        "估计命中和预计时间只用于选候选，最终仍由官方问题三评估器择优并保留上阶段回退。"
    )
    add_heading(doc, "6.3 全量配对结果与失效边界", 2)
    assert len(data) == 500 and len(curve) == 5
    assert all(r["baseline_legal"] == "True" and r["final_legal"] == "True" for r in data)
    tab = []
    for r in sorted(curve, key=lambda x: int(x["cores"])):
        tab.append([
            r["cores"],
            f"{float(r['mean_of_case_ratios_hardware_speedup_baseline']):.4f}",
            f"{float(r['mean_of_case_ratios_hardware_speedup_final']):.4f}",
            f"{float(r['mean_of_case_ratios_l2_plan_tuning_speedup']):.4f}",
            f"{float(r['mean_of_case_parallel_speedups_final_l2_vs_k1']):.4f}",
        ])
    chinese_table_header(add_table(
        doc, ["核数", "冻结方案硬件比", "最终方案硬件比",
              "L2 下方案优化比", "最终 L2 并行加速比"],
        tab, "表6 问题三 2×2 配对结果的逐例比值算术均值", font_size=10))
    image(doc, FIG / "q3" / "result_q3_scaling.png",
          "图8 冻结与最终方案在有/无 L2 下的 1—5 核曲线；同案例单核分母")
    add_paragraph(
        doc,
        "表6和图8给出四条并行曲线。以五核为例，"
        "同一最终方案的无/有 L2 官方完工时间比均值为 1.0248，"
        "而在有 L2 的同一硬件下，冻结/最终方案的时间比均值为 1.0121。"
        "前者是硬件效应，后者是方案优化效应；"
        "若改用“均值完工时间之比”，结果并非上述数值。"
        "问题三并行加速比的单核分母固定为 Stage 1 请求一核时的赢家，"
        "与问题二最终 single 基准不混写。"
    )
    image(doc, FIG / "q3" / "result_q3_effects.png",
          "图9 问题三同方案 L2 硬件效应与同硬件方案优化效应的逐例分布")
    add_paragraph(
        doc,
        "图9显示，L2 的作用并不由命中次数单独决定。"
        "冻结方案的 case_050/k4 在有 L2 后从 46,748 缩短到 44,398 cycle；"
        "而 case_067/k2 从 31,892,152 增至 32,130,317 cycle，"
        "尽管后者仍出现 L2 命中。"
        "官方时间线中，FIFO 淘汰、DDR 竞争和缓存读队列共同影响关键路径；"
        "这一反例界定了“有命中即提速”说法的边界。"
    )
    image(doc, FIG / "q3" / "result_q3_holdout.png",
          "图10 问题三 70 个留出案例中最终 L2 方案相对冻结 L2 方案的配对收益")
    add_paragraph(
        doc,
        "如图10所示，在预注册的 70 个留出案例中，2—5 核的最终方案相对冻结方案"
        "分别在 29、37、32、35 个 case-core 缩短了有 L2 的官方 Makespan，"
        "没有被选中的退化组。"
        "这是官方回退条件下的组合效果；其分布随核数和图结构而变，"
        "不能据此宣称 L2 专用排序或映射在所有案例上单独有效。"
    )


def discussion(doc) -> None:
    add_heading(doc, "7 讨论：收益来源、稳健性与适用边界", 1)
    add_paragraph(
        doc,
        "三问的共同线索是把结构判断与官方评分分开。"
        "依赖层和张量边界帮助提出可行切图，但问题二的共享 DDR 与 Spill"
        "使场景 A 的优胜者不再必然优胜。"
        "固定切图映射只在部分 case-core 上改善，阶段一全局回退把这种不稳定性"
        "转化为不退化的最终组合。"
        "问题三的 2×2 设计进一步表明，硬件本身的收益和针对硬件改方案的收益"
        "属于两个不同问题；把它们分开，才能解释命中率较高却没有缩短关键路径的案例。"
    )
    add_paragraph(
        doc,
        "本文的结果限定于题目提供的 100 张图、固定配置和仓库中的官方评估器。"
        "问题二的最终搜索没有改变切图边界，故对由边界和张量寿命主导的 Spill"
        "只能部分缓解；问题二排序 Gate A 的三例零最终改善也只足以停止该组预注册候选，"
        "不足以否定全部排序方法。"
        "问题三的 L2 模型是官方指定的只读 FIFO，而非任意实际硬件缓存。"
        "若图规模、带宽、容量或淘汰规则改变，当前候选权重与收益分布都需要重新评估。"
        "下一步最有区分度的检验，是在独立案例上比较有界切图修补"
        "与固定切图迁核，并保持排序、映射和硬件效应各自的配对消融。"
    )
    add_heading(doc, "8 结论", 1)
    add_paragraph(
        doc,
        "本文构建了从合法切图、场景 B 核映射到共享 L2 配对评估的一套可复核流程。"
        "在 100 个案例的官方评估中，五核的场景 A、场景 B 平均逐例加速比"
        "分别为 2.8905 和 3.2347；场景 B 最终组合在 124 个多核组"
        "缩短了阶段一实际赢家的完工时间，且保留零退化回退。"
        "问题三把同方案 L2 硬件效应与同硬件方案优化效应分开报告，"
        "五核对应比值均值为 1.0248 和 1.0121。"
        "这些结果支持“近似模型筛选、合法性校验、官方仿真择优”的闭环，"
        "而不支持用单一通信量、Spill 或命中率替代最终 Makespan。"
    )
    add_heading(doc, "参考文献", 1)
    add_paragraph(
        doc,
        "[1] TOPCUOGLU H, HARIRI S, WU M Y. Performance-effective and "
        "low-complexity task scheduling for heterogeneous computing[J]. "
        "IEEE Transactions on Parallel and Distributed Systems, 2002, 13(3): 260–274. "
        "DOI: 10.1109/71.993206.",
        indent=False, size=12,
    )
    add_paragraph(
        doc,
        "[2] 中国研究生数学建模竞赛组织委员会. 2026 年“华为杯”第二十三届中国研究生"
        "数学建模竞赛 A 题：通用神经网络处理器下的多核调度问题[Z]. 2026.",
        indent=False, size=12,
    )
    add_paragraph(
        doc,
        "AI 工具使用披露（审阅待核）：本稿的语言整理、图表核对及部分脚本编写有 AI 辅助；"
        "参赛队需逐段理解并用自己的语言修订。工具名称、型号、开发机构和版本发布日期"
        "待全队核实后，在涉及的数据分析结果前后及代码附件文件头补齐；"
        "未补齐前本稿不得作为最终提交版。",
        indent=False, size=12,
    )


def appendices(doc, q1_source, q2: list[dict[str, str]], q3: list[dict[str, str]]) -> None:
    doc.add_page_break()
    copy_problem1_appendix(doc, q1_source)
    doc.add_page_break()
    add_heading(doc, "附录 B 问题二 100 个案例的逐例官方结果", 1)
    add_paragraph(doc, "下列表格按案例列出 1—5 核最终方案的官方 Makespan、新增搬运与 Spill。"
                  "完整的候选失败记录、跨核传输和每核利用率保存在随附结果 CSV，"
                  "表中不是只抽取改善案例。")
    q2_by_case = {r["case"]: {} for r in q2}
    for r in q2:
        q2_by_case[r["case"]][int(r["cores"])] = r
    assert len(q2_by_case) == 100 and all(len(v) == 5 for v in q2_by_case.values())
    for field, title in [
        ("makespan_cycles", "表 B1 问题二最终 Makespan（cycle）"),
        ("added_copy_bytes", "表 B2 问题二最终新增搬运（byte）"),
        ("spill_added_copy_bytes", "表 B3 问题二最终 Spill（byte）"),
    ]:
        body = [[case, *[f"{int(q2_by_case[case][k][field]):,}" for k in range(1, 6)]]
                for case in sorted(q2_by_case)]
        table = add_table(
            doc, ["案例", "1 核", "2 核", "3 核", "4 核", "5 核"],
            body, title, font_size=8,
            widths_cm=[2.6, 2.8, 2.8, 2.8, 2.8, 2.8])
        chinese_table_header(table)
        compact_numeric_table(table)
    doc.add_page_break()
    add_heading(doc, "附录 C 问题三 2×2 逐例官方结果", 1)
    add_paragraph(doc, "下列四表分别固定方案与硬件条件，覆盖同一 100 个案例和 1—5 核。"
                  "表 C1/C2 是冻结的 Stage 1 方案；表 C3/C4 是 L2 调整后最终方案。"
                  "它们可逐格复算硬件效应与方案优化效应。")
    add_paragraph(
        doc,
        "复现入口与身份：问题一使用 run_problem1_full.py、run_problem1_inherited_fallback.py；"
        "问题二使用 run_problem2_baseline.py、run_problem2_stage2_mapping.py 和 "
        "run_problem2_final.py；问题三使用 run_problem3_g1.py、run_problem3_g2.py、"
        "report_problem3.py。输入为 data/case_001.json 至 case_100.json 和 data/config.txt。"
        "官方评估器位于 code/multicore_cut_evaluate_problem_1.py、_2.py、_3.py，"
        "求解与官方模块应连同完整代码附件一起提交；"
        "所有重跑应写入独立目录，不覆盖论文所据的冻结结果。"
    )
    q3_by_case = {r["case"]: {} for r in q3}
    for r in q3:
        q3_by_case[r["case"]][int(r["cores"])] = r
    assert len(q3_by_case) == 100 and all(len(v) == 5 for v in q3_by_case.values())
    for field, title in [
        ("baseline_no_l2_makespan", "表 C1 冻结方案无 L2 的 Makespan（cycle）"),
        ("baseline_l2_makespan", "表 C2 冻结方案有 L2 的 Makespan（cycle）"),
        ("final_no_l2_makespan", "表 C3 最终方案无 L2 的 Makespan（cycle）"),
        ("final_l2_makespan", "表 C4 最终方案有 L2 的 Makespan（cycle）"),
    ]:
        body = [[case, *[f"{int(q3_by_case[case][k][field]):,}" for k in range(1, 6)]]
                for case in sorted(q3_by_case)]
        table = add_table(
            doc, ["案例", "1 核", "2 核", "3 核", "4 核", "5 核"],
            body, title, font_size=8,
            widths_cm=[2.6, 2.8, 2.8, 2.8, 2.8, 2.8])
        chinese_table_header(table)
        compact_numeric_table(table)


def main() -> None:
    assert TEMPLATE.is_file() and Q1_DRAFT.is_file()
    q1_rows = rows(FIG / "q1" / "tables" / "problem1_inherited_fallback_results.csv")
    q2_rows = rows(FIG / "q2" / "tables" / "selected_results.csv")
    q2_agg = rows(FIG / "q2" / "tables" / "aggregate_by_core.csv")
    q3_rows = rows(FIG / "q3" / "tables" / "case_core_2x2.csv")
    q3_curve = rows(FIG / "q3" / "tables" / "curve_1_to_5.csv")
    assert len(q1_rows) == len(q2_rows) == len(q3_rows) == 500
    assert all(r["officially_measured"] == "True" for r in q1_rows)
    assert all(r["status"] == "success" for r in q2_rows)
    assert len({(r["case"], r["cores"]) for r in q3_rows}) == 500

    doc = Document(TEMPLATE)
    make_cover(doc)
    front_matter(doc)
    introduction(doc)
    q1_source = Document(Q1_DRAFT)
    copy_problem1_body(doc, q1_source)
    problem2(doc, q2_rows, q2_agg)
    problem3(doc, q3_rows, q3_curve)
    discussion(doc)
    appendices(doc, q1_source, q2_rows, q3_rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print(json.dumps({
        "output": str(OUT),
        "paragraphs": len(doc.paragraphs),
        "tables": len(doc.tables),
        "inline_figures": len(doc.inline_shapes),
        "case_core_rows_per_question": 500,
        "cover_school": doc.tables[0].cell(0, 1).text,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
