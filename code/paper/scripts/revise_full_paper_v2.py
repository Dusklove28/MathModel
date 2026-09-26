"""Revise the frozen V1 review DOCX without changing experiments or V1.

This is a paper-only transformation. The ten C tables are generated directly
from the 500 frozen official Problem-3 case-core records.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

import build_full_paper_v1 as base


ROOT = Path(__file__).resolve().parents[3]
V1 = ROOT / "code/paper/draft/2026华为杯A题完整论文V1-审阅.docx"
V2 = ROOT / "code/paper/draft/2026华为杯A题完整论文V2.docx"
Q3 = ROOT / "figures/q3/tables/case_core_2x2.csv"
DOCX_TOOLS = Path.home() / ".codex/skills/math-modeling/tools/docx/scripts"
sys.path.insert(0, str(DOCX_TOOLS))
import paper_format  # noqa: E402  -- official-template-compatible OMML builder


def only(doc, prefix: str):
    matches = [p for p in doc.paragraphs if p.text.startswith(prefix)]
    if len(matches) != 1:
        raise AssertionError(f"expected one paragraph beginning {prefix!r}: {len(matches)}")
    return matches[0]


def replace(doc, prefix: str, value: str, *, heading: bool = False) -> None:
    paragraph = only(doc, prefix)
    paragraph.clear()
    if heading:
        level_one = value[0].isdigit() and value[1:2] == " "
        base.east_asian_font(paragraph.add_run(value),
                             "黑体" if level_one else "宋体",
                             14 if level_one else 12, True)
    else:
        base.east_asian_font(paragraph.add_run(value), "宋体", 12)


def after(doc, anchor, value: str, *, heading: int = 0):
    anchor = only(doc, anchor) if isinstance(anchor, str) else anchor
    new = (base.add_heading(doc, value, heading) if heading
           else base.add_paragraph(doc, value))
    anchor._element.addnext(new._element)
    return new


def formula_after(doc, anchor, latex: str, number: int):
    anchor = only(doc, anchor) if isinstance(anchor, str) else anchor
    paragraph = paper_format.equation(doc, latex)
    paragraph.paragraph_format.space_after = Pt(3)
    base.east_asian_font(paragraph.add_run(f"    （{number}）"), size=10)
    anchor._element.addnext(paragraph._element)
    return paragraph


def append_sentence(doc, prefix: str, sentence: str) -> None:
    paragraph = only(doc, prefix)
    base.east_asian_font(paragraph.add_run(sentence), "宋体", 12)


def main() -> None:
    if not V1.is_file() or not Q3.is_file():
        raise FileNotFoundError("frozen V1 or Problem-3 2x2 table is unavailable")
    with Q3.open("r", encoding="utf-8-sig", newline="") as stream:
        q3 = list(csv.DictReader(stream))
    if len(q3) != 500 or len({(r["case"], r["cores"]) for r in q3}) != 500:
        raise AssertionError("Problem-3 official table must have 500 unique cells")

    doc = Document(V1)

    # Abstract and opening: one argument, with separate official metric scopes.
    replace(doc, "给定具有算子依赖",
            "给定具有算子依赖、双计算流水线和有限核内存储的计算图，增加核心数并不能直接保证缩短完工时间。"
            "本文把多核调度拆为三个递进决策：先确定并行任务的结构，再在共享 DDR 和缓存约束下映射子图，"
            "最后研究共享只读 L2 如何改变输入复用。各阶段用依赖、通信与资源负载构造可解释的候选，"
            "逐一检查方案合法性，并仅以对应场景官方模拟器给出的 Makespan 选择最终方案；"
            "低核优胜方案继承及阶段性回退限制了候选失效带来的退化。问题三固定同一批场景 B 初始方案，"
            "以方案×硬件的配对评估区分增加 L2 本身与调整调度方案的收益。")
    replace(doc, "本文的做法是先形成少量",
            "整体求解遵循“结构划分—资源映射—存储层次感知”的顺序。问题一用完整依赖层、"
            "弱连通分支和边界张量构造不同粒度的合法切图，并用商图上的上行秩和最早完成时间分核；"
            "这一排序框架借鉴 HEFT[1]，并非本文独立发明。问题二以这些切图为起点，"
            "重估共享资源下的核映射；问题三保持场景 B 初始方案不变，以同方案有/无 L2 的比较测量硬件效应，"
            "再在同一有 L2 条件下比较方案调整。每个近似量都只用于提出候选，不能代替官方时间线。"
            "图1展示三问的依赖关系。")
    replace(doc, "实验覆盖 100 张图",
            "实验覆盖题目给定的 100 张计算图和 1—5 核，共 500 个案例—核数组合。"
            "开发、验证和留出案例在利用结果调整规则之前划分；同一案例、同一核数的方案用官方结果配对比较。"
            "不合法候选不参与择优，估计值只用于缩小候选集合，最终性能主张均采用官方逐例 Makespan。"
            "完整逐例结果列于附录，复现文件与命令集中说明。")

    # Problem 1: task formulation and the mechanism behind its partition.
    replace(doc, "场景 A 中，一个子图",
            "问题一同时决定算子所属子图、每个子图所在核心及每核合法的子图执行顺序。"
            "切分过细可增加并行机会，却会放大跨子图数据周转和同步；切分过粗虽减少边界，"
            "又可能使关键路径集中在少数核心。故仅把算子工作量均摊到各核不能保证完工时间最短。"
            "本文在算子唯一归属、商图无环、每个子图只执行一次和核内顺序满足依赖的条件下，"
            "按（官方 Makespan，官方新增搬运字节）作字典序选择。")
    after(doc, "问题一同时决定算子所属子图",
          "完整依赖层提供天然的拓扑分界，窗口数控制并行粒度；窗口内保留弱连通分支，"
          "避免任意截断已有的局部依赖链。边界张量感知打包衡量不同分支之间潜在的数据耦合，"
          "弥补单纯按计算量切分的不足。切图决定任务粒度，随后商图的最早完成时间调度决定核归属；"
          "二者承担不同决策，不能把后者的收益全部归给某一种切图。")
    replace(doc, "4.3 完整依赖层窗口", "4.3 结构化切图：完整依赖层窗口与分支保持（B2A）", heading=True)
    replace(doc, "B2A 首先给算子",
            "结构化切图方案 B2A 首先给算子标记依赖层：源算子为第 0 层，"
            "其余算子的层号比全部直接前驱的最大层号大 1。由于每条依赖边均指向更高层，"
            "完整层窗口形成合法的拓扑候选边界，而不是为分层本身增加模型复杂度。"
            "实现对每层分别求 M、V 管线周期和，取两者较大值作为该层切分权重，"
            "再按累计权重形成连续、非空的工作量均衡窗口；只考察预设的 4、8、16 个窗口。")
    replace(doc, "每个案例在 2—5 核",
            "每个案例在 2—5 核下比较单子图、B0、B1 及 B2A 的 4/8/16 窗口六类候选。"
            "不同标签若生成同一正式方案，只评估一次。有效方案均交由官方场景 A 评估器计算，"
            "先比较 Makespan，相同时比较新增搬运，仍相同则按固定候选顺序打破并列。"
            "单核整图方案单独作为逐例加速比的基准。")
    append_sentence(doc, "在原始六类候选的 400 个多核组合中",
                    "低核继承的逐核配对结果见表3。")

    # Problem 2: distinguish the bounded proxy from the event simulator.
    replace(doc, "5.1 从子图商图", "5.1 场景 B 的决策与资源约束", heading=True)
    replace(doc, "问题二沿用问题一生成",
            "场景 B 把同一核心的全部子图并入一个任务，同核中间张量可以驻留，跨核依赖则需写入共享 DDR、"
            "同步并在目标核读入。问题二以问题一形成的五类非单子图切图为起点，优化变量是在固定切图下"
            "各子图的目标核心及合法核内顺序；算子归属不在最终映射步骤中改变。"
            "输入按消费核心去重读取，普通跨核张量按（张量、源核心、目标核心）去重，"
            "直接算子依赖仍按官方实现逐边计入。核内 M/V 管线、输出写回、"
            "驻留容量、Spill 和共享 DDR 排队共同决定时间线，故减少跨核字节或消除 Spill 都不是最终目标。")
    after(doc, "场景 B 把同一核心",
          "形式上，给定切图的子图集合 S 与请求核心数 k，令 c(s)∈{1,…,k} 表示子图 s 的核心，"
          "每核序列须是与全局依赖相容的拓扑顺序。每个子图只能出现一次，"
          "由映射和顺序重建出的官方方案还须满足容量和接口合法性。"
          "最终目标是在这些合法方案中使官方 Makespan 最小；仅当该时间相同时，"
          "用官方新增搬运量打破并列。")
    replace(doc, "候选筛选器使用子图",
            "为避免对每一种核分配都运行完整事件模拟，候选筛选器分别估计前驱释放后的完工时刻、"
            "每核 M/V Pipe 负载以及新增读写字节形成的共享 DDR 工作量下界，取三者的最大值作为排序代理。"
            "模型还估计张量的核内驻留峰值与容量超额；这一超额只提示 Spill 风险，"
            "不能等同于官方插入的换入换出量。预计跨核等待仅在具体依赖上加入固定同步延迟，"
            "绝不以延迟乘全部传输次数估算 Makespan。")
    q2_proxy_equation = formula_after(doc, "为避免对每一种核分配",
                                      r"T_{proxy}=\max(T_{dep},T_{pipe},T_{DDR})", 7)
    after(doc, q2_proxy_equation,
          "式（7）中的 DDR 项为估计读写字节除以共享带宽所得工作量下界，"
          "并未重现多个 COPY 在实际发射时刻的并发排队；因此代理值既不是官方完工时间，"
          "也不作为最终方案比较的依据。")
    replace(doc, "5.2 有界局部映射", "5.2 基于通信局部性与流水线负载的有界映射", heading=True)
    replace(doc, "对五类非 single 切图",
            "对每种固定切图，先保留原映射，再按商图拓扑顺序逐个处理就绪子图。"
            "试探每个核心时，先比较预计最早完工，再依次比较跨核载荷、跨核传输数、"
            "该核心新增输入读取和 M/V Pipe 最大负载；核号只用于确定性打破并列。"
            "随后从具有依赖邻接的子图中按关键性与负载选取至多 12 个迁核考察对象，"
            "最多进行两次单子图迁核。"
            "全局代理的比较顺序为预计完工、跨核载荷、传输数、预计总搬运、最大容量超额。"
            "这是一种有限预算的核映射搜索，不改变切图边界，也不保证代理最优等于官方最优。")
    replace(doc, "每个多核组合至少比较",
            "候选池保留单子图及五类原映射，同时加入各切图的场景 B 专用映射和经合法性复评的低核优胜方案。"
            "代理搜索结束后重建最终候选方案，检查子图 DAG 与核内顺序，"
            "再由官方接口检验和评估；该案例—核数组合的初始官方赢家始终可回退。"
            "只有新方案的（官方 Makespan，官方新增搬运）严格更优时才替换。"
            "固定映射下的核内顺序小样本探索未带来最终组合收益，因此未继续扩大该分支；"
            "这一资源决策不能解释为所有排序方法均无效。")
    after(doc, "5.3 全量结果与严格配对比较",
          "各核数的 100 例官方指标汇总见表4，随后按同案例同核数比较新增映射与初始实际赢家。")
    append_sentence(doc, "表4和图5表明", "冻结分层的严格配对结果见表5。")
    after(doc, "表4和图5表明",
          "从整体趋势看，五核平均加速比虽高于二核，但并未随核数线性增长。"
          "在共享 DDR、依赖等待和核内存储共同限制下，某些图的额外核心保持空闲或仅带来很小收益；"
          "图5的逐例分布比均值更能显示这种异质性。这里不能仅凭曲线断言瓶颈必然来自某一种资源。")
    replace(doc, "图7进一步表明",
            "图7中的配对关系说明，Spill 或跨核有效载荷下降不必然伴随 Makespan 下降。"
            "DDR 的总字节数之外，输入重复读取、输出写回、跨核同步在何时落到关键路径上也重要。"
            "官方时间线综合这些事件后才给出最终结果，因此我们只把搬运与 Spill 作为诊断信号。"
            "固定切图上的核映射可为部分图提速，却难以修复由切图边界决定的长期驻留压力；"
            "本稿没有把未经全量验证的边界修补计入最终方法。")
    append_sentence(doc, "表5和图6显示",
                    "图7进一步把新增搬运、Spill 与完工时间放在同组方案中比较。")

    # Problem 3: distinguish the two controlled comparisons and the proxy.
    replace(doc, "6.1 同方案硬件效应", "6.1 共享 L2 语义与 2×2 配对设计", heading=True)
    replace(doc, "问题三使用问题二 Stage 1",
            "问题三从场景 B 第一阶段已冻结的 500 个实际赢家方案出发，"
            "而不是混用问题二后来调整的最终映射。官方 L2 为全部核心共享的只读缓存，容量 1 MiB、"
            "读速率 250 byte/cycle；COPY_IN 发起时查询，DDR 搬运完成时填充，按 FIFO 淘汰。"
            "L2 与 DDR 带宽独立，但命中仍占用 L2 读队列，未命中继续竞争 DDR。"
            "因此命中可以减少部分 DDR 读，却不保证关键路径缩短。")
    replace(doc, "第二条比较在同一有 L2",
            "每个案例与核数均评估“冻结方案/最终方案 × 无 L2/有 L2”的四格组合。"
            "同一最终方案的无/有 L2 时间比衡量只改变硬件条件的效应；"
            "同一有 L2 条件下冻结/最终方案的时间比衡量方案调整的效应。"
            "两种比较的分母不同，不能相乘或称为某个单一策略的独立收益。"
            "四格都由官方模拟器重新评估，并保留方案不改时的配对基线。")
    hardware_equation = formula_after(doc, "每个案例与核数均评估",
                                      r"R_{hardware}=\frac{T_{final,0}}{T_{final,1}}", 8)
    policy_equation = formula_after(doc, hardware_equation,
                                    r"R_{policy}=\frac{T_{base,1}}{T_{final,1}}", 9)
    after(doc, policy_equation,
          "式（8）、式（9）中 0/1 分别表示无/有 L2，base/final 表示冻结/最终方案。"
          "先对每个案例分别求比，再对 100 个案例取算术平均；"
          "这不同于先求平均 Makespan 再取比值。")
    replace(doc, "6.2 L2 复用窗口顺序", "6.2 L2 复用感知排序与共享输入迁核", heading=True)
    replace(doc, "G2 先固定冻结切图",
            "第一步固定切图和核映射，只调整合法核内子图顺序。"
            "对每个已就绪子图，候选评分估计输入发起时的 FIFO 命中、DDR/L2 各自的服务结束、"
            "跨核前驱释放、计算完成、关键路径优先级和核内容量超额。"
            "两种确定性排序使用不同的关键路径与复用权重，外层还先区分是否超容，"
            "再按评分、预计完工、关键度、核心号和子图号打破并列。"
            "这只是候选排序，不是官方 L2 的精确重演。第二步在排序后的方案上寻找共享输入分散到多核的机会，"
            "最多保留两个单子图迁核候选；每一步均重建方案、检查合法性并交由官方评估，劣者回退。")
    after(doc, "6.3 全量配对结果与失效边界",
          "100 例的四格配对均值见表6，各核数的并行曲线见图8。")
    replace(doc, "表6和图8给出",
            "表6和图8给出四格配对及并行曲线。以五核为例，同一最终方案的无/有 L2 官方"
            "完工时间比的逐例均值为 1.0248；在有 L2 的同一硬件下，冻结/最终方案的时间比均值为 1.0121。"
            "二者分别对应硬件效应与方案效应，收益幅度都较小，不能相加为总加速。"
            "并行加速比的单核分母采用本问冻结基线的一核赢家，与问题二最终组合基准不混写。")
    append_sentence(doc, "表6和图8给出",
                    "两种配对效应的逐例分布见图9。")
    for old, new in (("表4和图5表明", "从表4和图5可见"),
                     ("表5和图6显示", "从表5和图6可见"),
                     ("表6和图8给出", "从表6和图8可见")):
        paragraph = only(doc, old)
        updated = paragraph.text.replace(old, new, 1)
        paragraph.clear()
        base.east_asian_font(paragraph.add_run(updated), "宋体", 12)
    after(doc, "图9显示",
          "这两个案例提供方向相反的同方案观察，但尚不足以唯一确定造成增减速的具体事件。"
          "合理的优化目标是关键路径上的有效访存等待，而不是把 Cache 命中率本身最大化。"
          "附录同时列出逻辑新增搬运和按字节计的 L2 命中率，以免用命中次数替代实际完工时间。")

    # Discussion: each paragraph has a separate job instead of a results replay.
    replace(doc, "7 讨论：", "7 讨论与模型评价", heading=True)
    after(doc, "7 讨论与模型评价", "7.1 三问方法的递进关系", heading=2)
    replace(doc, "三问的共同线索是",
            "三问并非三套互不相干的启发式。问题一决定并行任务的结构与粒度，"
            "问题二在共享 DDR 和有限核内存储下重新安排这些任务，"
            "问题三检验加入共享 L2 后硬件变化与方案变化各贡献多少。"
            "在每一步，近似量提出少量候选，合法性检查排除不可执行方案，"
            "官方模拟器决定最终 Makespan；回退使局部规则失效时不必强行接受更差方案。"
            "这一闭环比任何单个贪心排序规则更能解释最终结果。")
    after(doc, "三问并非三套", "7.2 收益来源与失效边界", heading=2)
    after(doc, "7.2 收益来源",
          "增加核心、减少跨核载荷、降低 Spill、提高 L2 命中率分别改变的是资源条件或中间指标，"
          "并非直接改变最终目标。只有这些变化恰好缩短关键路径上的等待或执行，"
          "才可能转化为 Makespan 收益。场景 B 的部分映射未获益、L2 反例以及低核优胜方案的继承，"
          "共同界定了这一判断的边界；这里的机理解释与官方配对结果相容，但未完成逐事件的因果分解。")
    after(doc, "增加核心、减少跨核载荷", "7.3 局限与可检验的改进", heading=2)
    replace(doc, "本文的结果限定于",
            "结论只覆盖题目给定的 100 张图、固定配置和官方模拟器。"
            "问题一只比较 4/8/16 个窗口，无法回答自适应窗口是否更优；"
            "问题二最终搜索固定切图边界，因此不能消除由边界与张量寿命造成的全部驻留压力；"
            "问题三只适用于题设 1 MiB、FIFO、只读 L2。"
            "代理没有精确重演共享 DDR 的并发排队或缓存事件，因此不宜脱离官方评估器单独部署。"
            "后续最有价值的检验是：在独立图集上有预算地联合修补切图与映射，"
            "并改变 L2 容量与淘汰方式观察同方案效应是否稳定；这些实验本稿未做。")
    replace(doc, "本文构建了从合法切图",
            "本文把多核计算图调度分为结构划分、共享资源下的核映射和 L2 感知调度，"
            "用可解释代理提出候选，再通过合法性检查与官方模拟器择优。"
            "100 例官方评估中，五核场景 A、场景 B 的平均逐例加速比分别为 2.8905、3.2347；"
            "场景 B 的 400 个多核组合有 124 组比初始实际赢家更快，其余由并列或回退保护。"
            "问题三在五核下的同方案 L2 硬件比与同硬件方案比的逐例均值分别为 1.0248、1.0121。"
            "这些结果说明切图、映射、存储层次必须按各自资源语义评价；"
            "其有效范围仍限于本题计算图、配置及官方评分实现。")

    # Caption repairs; captions sit under figures and must stand alone.
    captions = {
        "图3 2—5 核": "图3 问题一 100 例在 2—5 核下的逐例加速比分布；分母为同案例单核官方时间",
        "图4 继承回退": "图4 问题一低核优胜方案继承带来的 36 个严格改善组合；比较同案例同核数的官方 Makespan",
        "图6 问题二 70": "图6 问题二留出集 70 例在 2—5 核下相对阶段一实际赢家的 Makespan 改善；零收益组保留",
        "图7 场景 B": "图7 场景 B 同案例同核数的新增搬运、Spill 变化与官方 Makespan 变化；以阶段一实际赢家为基准",
        "图8 冻结与最终": "图8 问题三冻结与最终方案在无/有 L2 下的 1—5 核平均逐例并行加速比；分母为本问同案例一核基线",
        "图9 问题三同方案": "图9 问题三同方案无/有 L2 时间比与同硬件冻结/最终方案时间比的逐例分布",
        "图10 问题三 70": "图10 问题三 70 个留出案例中最终方案相对冻结方案的有 L2 官方 Makespan 改善分布",
    }
    for prefix, value in captions.items():
        paragraph = only(doc, prefix)
        paragraph.clear()
        base.east_asian_font(paragraph.add_run(value), "宋体", 12)

    # Appendix C: keep all four 2x2 Makespan tables and complete the official
    # per-case movement / cache requirements for each interpretable cell.
    replace(doc, "下列四表分别固定",
            "C1—C4 列出冻结/最终方案在无/有 L2 条件下的逐例官方 Makespan；"
            "C5—C8 按相同四格列出总额外数据搬运量；C9—C10 分别列出两种有 L2 方案的按字节计 Cache 命中率。"
            "无 L2 条件不存在 Cache 命中率，记为不适用。每表覆盖同一 100 个案例、1—5 核，"
            "可按行复算两种配对比值；表中数值与正文使用同一份官方结果。")
    replace(doc, "复现入口与身份：",
            "三问的真实源码入口、固定配置与结果文件统一列于附录 D；"
            "这些逐例表格仅记录原版官方评估器的数值，不记录候选代理值。")

    by_case: dict[str, dict[int, dict[str, str]]] = {}
    for row in q3:
        by_case.setdefault(row["case"], {})[int(row["cores"])] = row
    if len(by_case) != 100 or any(len(v) != 5 for v in by_case.values()):
        raise AssertionError("each Problem-3 case must cover all five core counts")
    specs = [
        ("baseline_no_l2_added_copy_bytes", "表 C5 冻结方案无 L2 的总额外搬运（byte）", "bytes"),
        ("baseline_l2_added_copy_bytes", "表 C6 冻结方案有 L2 的总额外搬运（byte）", "bytes"),
        ("final_no_l2_added_copy_bytes", "表 C7 最终方案无 L2 的总额外搬运（byte）", "bytes"),
        ("final_l2_added_copy_bytes", "表 C8 最终方案有 L2 的总额外搬运（byte）", "bytes"),
        ("baseline_l2_cache_hit_rate_bytes", "表 C9 冻结方案有 L2 的字节命中率（%）", "percent"),
        ("final_l2_cache_hit_rate_bytes", "表 C10 最终方案有 L2 的字节命中率（%）", "percent"),
    ]
    for field, title, kind in specs:
        table_rows = []
        for case in sorted(by_case):
            values = []
            for cores in range(1, 6):
                raw = by_case[case][cores][field]
                if not raw.strip():
                    raise AssertionError(f"missing official value: {case}/{cores}/{field}")
                value = f"{int(raw):,}" if kind == "bytes" else f"{100*float(raw):.2f}"
                values.append(value)
            table_rows.append([case, *values])
        table = base.add_table(doc, ["案例", "1 核", "2 核", "3 核", "4 核", "5 核"],
                               table_rows, title, font_size=8,
                               widths_cm=[2.6, 2.8, 2.8, 2.8, 2.8, 2.8])
        base.chinese_table_header(table)
        base.compact_numeric_table(table)

    base.add_heading(doc, "附录 D 真实程序与结果文件说明", 1)
    base.add_paragraph(doc,
        "问题一的结构化切图、商图调度和候选选择分别由 solver_problem1.py、"
        "candidate_manager_problem1.py 与相应运行入口实现；问题二的场景 B 代理和有界迁核见"
        "solver_problem2.py，最终回退见 candidate_manager_problem2_final.py；"
        "问题三的两套复用窗口排序和共享输入迁核见 solver_problem3.py、"
        "solver_problem3_mapping.py。三问都使用题目原版评估程序与固定 config.txt，"
        "绝不以本队代理值替代官方 Makespan。以上文件是完整可运行源码的定位信息，"
        "不能用不完整代码节选代替题目要求的可复现程序附件。",
        indent=False, align=WD_ALIGN_PARAGRAPH.LEFT)
    base.add_paragraph(doc,
        "逐例结果分别见 figures/q1/tables/appendix_problem1_wide.csv、"
        "figures/q2/tables/selected_results.csv 和 figures/q3/tables/case_core_2x2.csv。"
        "完整运行应从题目给定的 100 张输入图和统一配置开始；程序附件与论文 PDF 的命名、"
        "打包和上传由参赛队按当届系统要求最终确认。",
        indent=False, align=WD_ALIGN_PARAGRAPH.LEFT)

    V2.parent.mkdir(parents=True, exist_ok=True)
    doc.save(V2)
    print(json.dumps({"output": str(V2), "paragraphs": len(doc.paragraphs),
                      "tables": len(doc.tables), "figures": len(doc.inline_shapes),
                      "q3_appendix_tables": [x[1].split()[1] for x in specs],
                      "q3_case_core_rows": len(q3)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
