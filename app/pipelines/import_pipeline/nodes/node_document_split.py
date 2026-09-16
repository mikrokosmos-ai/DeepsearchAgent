import sys
import json
import os
import re
from pathlib import Path
from typing import Tuple, List, Dict
from langchain_text_splitters import RecursiveCharacterTextSplitter
from app.conf.import_pipeline_config import import_pipeline_config
from app.core.logger import logger, node_log, step_log
from app.pipelines.import_pipeline.state import ImportGraphState
from app.utils.task_utils import add_running_task, add_done_task

# ====================== 全局配置（来源：app/conf/import_pipeline_config.py）======================
CHUNK_MAX_SIZE = import_pipeline_config.chunk_max_size  # 500 触发二次切割
CHUNK_SIZE = import_pipeline_config.chunk_size  # 单块长度
CHUNK_OVERLAP = import_pipeline_config.chunk_overlap  # 块间重叠
CHUNK_MIN_SIZE = import_pipeline_config.chunk_min_size  # 最小块长度


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


@step_log("step_2_split_by_title")
def step_2_split_by_title(md_content, file_title) -> List[Dict[str, str]]:
    """
        语义切割,根据标题,进行内容切割!
        :return: [{content,title,file_title}]
    """
    #1. 定义正则
    rep = re.compile(r"^\s*#{1,6}\s+.+")
    #2. 根据\n进行行的切割
    lines = md_content.split("\n")
    #3. 准备一些数据容器
    chunks = []
    current_title = ""
    current_lines = []
    pending_titles = []
    is_code_block = False
    title_count = 0

    #4. 循环处理每行数据
    for line in lines:
        strip_line = line.strip()
        #5. 检查代码块进出
        if strip_line.startswith('```') or strip_line.startswith('~~~'):
            is_code_block = not is_code_block
            current_lines.append(line)
            continue

        #6. 判断是不是标题
        if re.match(rep, strip_line) and not is_code_block:
            if current_title:
                if current_lines:
                    head = "\n".join(pending_titles + [current_title])
                    body = "\n".join(current_lines)
                    chunks.append({
                        "content": f"{head}\n{body}",
                        "title": current_title,
                        "file_title": file_title,
                    })
                    pending_titles = []
                else:
                    pending_titles.append(current_title)
            else:
                if any(ln.strip() for ln in current_lines):
                    chunks.append({
                        "content": "\n".join(current_lines),
                        "title": file_title,
                        "file_title": file_title,
                    })
            current_title = strip_line
            current_lines = []
            title_count += 1
        else:
            current_lines.append(line)

    #8. 处理最后一个标题
    if current_title:
        if current_lines:
            head = "\n".join(pending_titles + [current_title])
            body = "\n".join(current_lines)
            chunks.append({
                "content": f"{head}\n{body}",
                "title": current_title,
                "file_title": file_title
            })
            pending_titles = []

    #9. 没有标题的文档
    if title_count == 0:
        chunks.append({
            "content": md_content,
            "title": "default",
            "file_title": file_title
        })
        title_count = 1
    #10.返回结果
    logger.info(f"完成语义切割,切块数量:{len(chunks)},内容:{chunks[:3]}")
    return chunks


@step_log("step_3_data_refine_chunk")
def step_3_data_refine_chunk(chunks) -> List[Dict[str, str]]:
    """
       作用: 将超过执行size的标题内容,进行二次切分,二次切分产生: parent_title part
    """
    #1. 定义langchain提供的递归切割器
    splitter = RecursiveCharacterTextSplitter(
        separators=["\n\n", "\n", "。", "！", "；", " ", ""],
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP
    )
    #2. 获取原来的chunks
    final_chunks = []
    for chunk in chunks:
        content = chunk['content']
        if len(content) > CHUNK_MAX_SIZE:
            #4. 超过阈值 -> 递归切割器 二次切割
            spliter_chunks = splitter.split_text(content)
            for text in spliter_chunks:
                if len(text) > CHUNK_MAX_SIZE:
                    logger.warning(f"切分后仍存在超长块(len={len(text)}),请检查 chunk_size 配置!")
            for index, text in enumerate(spliter_chunks, start=1):
                final_chunks.append({
                    "content": text,
                    "title": f'{chunk["title"]}_{index}',
                    "file_title": chunk["file_title"],
                    "part": index,
                    "parent_title": chunk["title"]
                })
        else:
            chunk['part'] = 0
            chunk['parent_title'] = chunk['title']
            final_chunks.append(chunk)
    return final_chunks


@step_log("step_4_merge_small_chunks")
def step_4_merge_small_chunks(chunks) -> List[Dict[str, str]]:
    """
       作用: 将低于最小阈值的相邻碎块合并,避免切分过碎导致语义不完整
    """
    merged_chunks = []
    accumulator = None

    for chunk in chunks:
        if accumulator is None:
            accumulator = dict(chunk)
            continue
        acc_len = len(accumulator["content"])
        if (accumulator["parent_title"] == chunk["parent_title"]
                and acc_len < CHUNK_MIN_SIZE
                and acc_len + len(chunk["content"]) < CHUNK_MAX_SIZE):
            accumulator["content"] = f'{accumulator["content"]}\n{chunk["content"]}'
            accumulator["title"] = chunk["title"]
            accumulator["part"] = chunk["part"]
        else:
            merged_chunks.append(accumulator)
            accumulator = dict(chunk)

    if accumulator is not None:
        if (len(accumulator["content"]) < CHUNK_MIN_SIZE
                and merged_chunks
                and merged_chunks[-1]["parent_title"] == accumulator["parent_title"]):
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
    "title": chunk标题
    "parent_title": 父标题
    "part": 部分数（0代表没有精切，其余数字代表精切后的第几部分）
    "content": chunk内容
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
    # 3. 按照标题进行数据切割
    chunks = step_2_split_by_title(md_content, file_title)
    # 4. 二次细分切割
    chunks = step_3_data_refine_chunk(chunks)
    # 5. 合并小碎块
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
    from app.pipelines.import_pipeline.nodes.node_md_img import node_md_img

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
