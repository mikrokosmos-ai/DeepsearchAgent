import sys
import json
import os
from pathlib import Path
from typing import Tuple, List, Dict
from langchain_text_splitters import RecursiveCharacterTextSplitter
from app.rag.conf.import_pipeline_config import import_pipeline_config
from app.core.logger import logger, node_log, step_log
from app.rag.pipelines.import_pipeline.state import ImportGraphState
from app.rag.utils.chunk_blocks import (
    BLOCK_CODE,
    BLOCK_IMAGE,
    BLOCK_LIST,
    BLOCK_TABLE,
    BLOCK_TEXT,
    Block,
    chunk_row,
    list_batches,
    section_prefix,
    slice_blocks,
    table_batches,
)
from app.utils.task_utils import add_running_task, add_done_task

CHUNK_MAX_SIZE = import_pipeline_config.chunk_max_size  # 500 触发二次切割
CHUNK_SIZE = import_pipeline_config.chunk_size  # 单块长度
CHUNK_OVERLAP = import_pipeline_config.chunk_overlap  # 块间重叠
CHUNK_MIN_SIZE = import_pipeline_config.chunk_min_size  # 最小块长度
# 章节路径取末几级标题（模板与正文前缀共用同一口径，保证长短块一致）
SECTION_LEVELS = import_pipeline_config.embedding_section_levels
# 表格超长分批时每批最多带多少数据行
TABLE_MAX_ROWS = import_pipeline_config.chunk_table_max_rows


@step_log("step_1_validate_clean")
def step_1_validate_clean(state) -> Tuple[str, str]:
    """
        1. 获取数据  md_content , file_title , md_path
        2. md_content进行非空校验 空->异常
        3. md_content不为空 -> 数据清洗
        4. file_title进行非空判断 -> 空 -> 通过md_path获取file_title
        5. 返回数据
    """
    md_content = state['md_content']
    file_title = state['file_title']
    md_path = state['md_path']

    # 进行必要非空校验
    if not md_content:
        logger.warning(f"没有从state读取到md_content内容,我们使用md_path尝试再次读取!")
        if md_path:
            md_content = Path(md_path).read_text(encoding='utf-8')
            state['md_content'] = md_content
        if not md_content:
            logger.error(f"md_content没数据,并且尝试读取md_path依然没有数据,终止执行!!")
            raise ValueError("md_content没数据,并且尝试读取md_path依然没有数据,终止执行!!")
    if not file_title:
        logger.warning("没有在state读取到file_title,计划给与默认值!")
        if md_path:
            file_title = Path(md_path).stem
        if not file_title:
            file_title = "default"
        state['file_title'] = file_title
    # md_content内容进行清洗
    md_content = md_content.replace("\r\n", "\n").replace("\r", "\n")
    return md_content, file_title


@step_log("step_2_slice_blocks")
def step_2_slice_blocks(md_content, file_title) -> List[Block]:
    """
    语义切割：按标题切 + 按块类型分组（表格 / 代码 / 列表 / 图片 / 普通段落各自成块）

    标题栈全量维护，每个块都带上完整章节路径 —— 它既是元数据字段的来源，
    也是长块补前缀的依据（旧实现只在「父标题没正文」时才会把标题带下去）。
    """
    blocks = slice_blocks(md_content, file_title)
    counts: Dict[str, int] = {}
    for block in blocks:
        counts[block.block_type] = counts.get(block.block_type, 0) + 1
    logger.info(f"完成语义切割,块数量:{len(blocks)},按类型:{counts}")
    return blocks


@step_log("step_3_blocks_to_chunks")
def step_3_blocks_to_chunks(blocks: List[Block], file_title: str) -> List[Dict]:
    """
    按块类型分发处理：

        表格   整表成块；超长时按行分批且**每批都重复表头**（数据行不丢不重）
        代码   不切（切断代码块等于毁掉可读性），超长只告警
        列表   尽量整段保留，真超长才按条目边界分批
        图片   与其所在段落一起整块保留
        段落   沿用递归切割器二次切；每片都补上章节路径前缀，长短块口径一致
    """
    splitter = RecursiveCharacterTextSplitter(
        separators=["\n\n", "\n", "。", "！", "；", " ", ""],
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP
    )
    chunks: List[Dict] = []

    for block in blocks:
        prefix = section_prefix(block.section_path, SECTION_LEVELS)
        head = f"{prefix}\n" if prefix else ""
        body = "\n".join(block.lines)
        budget = max(1, CHUNK_MAX_SIZE - len(head))

        if block.block_type == BLOCK_CODE:
            if len(head) + len(body) > CHUNK_MAX_SIZE:
                logger.warning(
                    f"代码块超过 CHUNK_MAX_SIZE({CHUNK_MAX_SIZE})，按设计保持整块不切："
                    f"len={len(head) + len(body)}，title={block.title!r}"
                )
            chunks.append(chunk_row(head + body, block, file_title, 0))
            continue

        if block.block_type == BLOCK_TABLE:
            if len(head) + len(body) <= CHUNK_MAX_SIZE:
                chunks.append(chunk_row(head + body, block, file_title, 0))
            else:
                for part, batch in enumerate(
                    table_batches(block.lines, TABLE_MAX_ROWS, budget), start=1
                ):
                    chunks.append(chunk_row(head + "\n".join(batch), block, file_title, part))
            continue

        if block.block_type == BLOCK_LIST:
            if len(head) + len(body) <= CHUNK_MAX_SIZE:
                chunks.append(chunk_row(head + body, block, file_title, 0))
            else:
                for part, batch in enumerate(list_batches(block.lines, budget), start=1):
                    chunks.append(chunk_row(head + "\n".join(batch), block, file_title, part))
            continue

        if block.block_type == BLOCK_IMAGE:
            chunks.append(chunk_row(head + body, block, file_title, 0))
            continue

        # 普通段落：短块整段留，长块二次切
        if len(head) + len(body) <= CHUNK_MAX_SIZE:
            chunks.append(chunk_row(head + body, block, file_title, 0))
        else:
            pieces = splitter.split_text(body)
            for part, piece in enumerate(pieces, start=1):
                if len(piece) > CHUNK_MAX_SIZE:
                    logger.warning(
                        f"切分后仍存在超长块(len={len(piece)}),请检查 chunk_size 配置!"
                    )
                chunks.append(chunk_row(head + piece, block, file_title, part))

    logger.info(f"分块完成,chunk 数:{len(chunks)}")
    return chunks


@step_log("step_4_merge_small_chunks")
def step_4_merge_small_chunks(chunks) -> List[Dict]:
    """
    合并低于最小阈值的相邻碎块，避免切分过碎导致语义不完整

    合并条件收紧到「同章节 + 同块类型」：跨章节合并会把两节的内容黏成一块，
    检索到它时无法判断答案属于哪一节；跨类型合并（例如把表格并进段落）会毁掉
    表格的整表语义。合并后 title 保持首块的标题（旧实现取后一块，是一处漂移）。
    """
    merged_chunks: List[Dict] = []
    accumulator = None

    def mergeable(prev: Dict, nxt: Dict) -> bool:
        return (
            prev.get("section_path") == nxt.get("section_path")
            and prev.get("block_type") == nxt.get("block_type")
        )

    for chunk in chunks:
        if accumulator is None:
            accumulator = dict(chunk)
            continue
        acc_len = len(accumulator["content"])
        if (mergeable(accumulator, chunk)
                and acc_len < CHUNK_MIN_SIZE
                and acc_len + len(chunk["content"]) < CHUNK_MAX_SIZE):
            accumulator["content"] = f'{accumulator["content"]}\n{chunk["content"]}'
            # part 取两者较大值：合并后覆盖到更靠后的位置，序号保持单调不回退
            accumulator["part"] = max(accumulator.get("part", 0), chunk.get("part", 0))
        else:
            merged_chunks.append(accumulator)
            accumulator = dict(chunk)

    if accumulator is not None:
        if (len(accumulator["content"]) < CHUNK_MIN_SIZE
                and merged_chunks
                and mergeable(merged_chunks[-1], accumulator)
                and len(merged_chunks[-1]["content"]) + len(accumulator["content"]) < CHUNK_MAX_SIZE):
            prev = merged_chunks[-1]
            prev["content"] = f'{prev["content"]}\n{accumulator["content"]}'
        else:
            merged_chunks.append(accumulator)

    logger.info(f"合并小文本块完成,合并前块数:{len(chunks)},合并后块数:{len(merged_chunks)}")
    return merged_chunks


@step_log("step_5_backup_data")
def step_5_backup_data(chunks, md_path):
    """
    将数据存储到本地 文件夹名 / chunks.json
    """
    chunks_json_path_obj = Path(md_path).parent / "chunks.json"
    chunks_json_path_obj.write_text(json.dumps(
        chunks,
        ensure_ascii=False,
        indent=4
    ), encoding="utf-8")


"""
chunk结构说明：
    "file_title": 去后缀的MD文件名
    "title": 当前块所属标题（序号只进 part，不再写「原标题_序号」伪标题）
    "parent_title": 与 title 同值（保留字段以兼容既有读取方）
    "part": 0=未切分；>0=二次切分后的第几片
    "section_path": 完整标题链（列表）
    "block_type": 块类型 text/table/code/list/image
    "content": chunk内容（长块子块的前缀是章节路径末两级，与向量模板同口径）
"""


@node_log("node_document_split")
def node_document_split(state: ImportGraphState) -> ImportGraphState:
    """
    节点: 文档切分 (node_document_split)
    为什么叫这个名字: 将长文档切分成小的 Chunks (切片) 以便检索。
    """
    # 1. 记录进行状态
    add_running_task(state['task_id'], sys._getframe().f_code.co_name, state.get("is_stream", False))
    # 2. 数据校验和清洗
    md_content, file_title = step_1_validate_clean(state)
    # 3. 按标题 + 块类型切块
    blocks = step_2_slice_blocks(md_content, file_title)
    # 4. 按块类型分发成 chunk（表格分批带表头 / 代码不切 / 长段落补章节前缀）
    chunks = step_3_blocks_to_chunks(blocks, file_title)
    # 5. 合并小碎块（同章节同类型）
    chunks = step_4_merge_small_chunks(chunks)
    # 6. 数据备份 chunks.json
    step_5_backup_data(chunks, state['md_path'])
    # 7. 修改state属性 chunks
    state["chunks"] = chunks
    # 8. 进行任务的管理 done_task
    add_done_task(state['task_id'], sys._getframe().f_code.co_name, state.get("is_stream", False))
    return state


if __name__ == '__main__':
    """本地测试入口"""
    from app.core.paths import PROJECT_ROOT
    from app.rag.pipelines.import_pipeline.nodes.node_md_img import node_md_img

    logger.info(f"本地测试 - 项目根目录：{PROJECT_ROOT}")

    test_md_name = os.path.join(r"output\hak180产品安全手册", "hak180产品安全手册.md")
    test_md_path = os.path.join(PROJECT_ROOT, test_md_name)

    if not os.path.exists(test_md_path):
        logger.error(f"本地测试 - 测试文件不存在：{test_md_path}")
    else:
        test_state = {
            "md_path": test_md_path,
            "task_id": "test_task_123456",
            "md_content": "",
            "file_title": "hak180产品安全手册",
            "local_dir": os.path.join(PROJECT_ROOT, "output"),
        }
        result_state = node_md_img(test_state)
        logger.info(f"本地测试完成 - 处理结果状态：{result_state}")
        logger.info("\n=== 开始执行文档切分节点集成测试 ===")
        final_state = node_document_split(result_state)
        final_chunks = final_state.get("chunks", [])
        logger.info(f"✅ 测试成功：最终生成{len(final_chunks)}个有效Chunk{final_chunks}")
