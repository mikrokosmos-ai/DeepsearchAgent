import sys
import os
from pathlib import Path

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from pymilvus import DataType
from app.rag.conf.milvus_config import milvus_config
from app.rag.pipelines.import_pipeline.state import ImportGraphState
from app.rag.clients.milvus_client import get_milvus_client
from app.rag.clients.llm_client import get_llm_client
from app.rag.clients.embedding_client import generate_embeddings
from app.utils.task_utils import add_running_task, add_done_task
from app.core.logger import logger, node_log, step_log
from app.prompts.loader import load_prompt
from app.rag.conf.import_pipeline_config import import_pipeline_config


# --- 配置参数 (Configuration，来源：app/conf/import_pipeline_config.py) ---
DEFAULT_ITEM_NAME_CHUNK_K = import_pipeline_config.item_name_chunk_k
# 历史遗留常量，当前代码未使用（step_2 只对拼接结果做总长度截断）
SINGLE_CHUNK_CONTENT_MAX_LEN = 800
CONTEXT_TOTAL_MAX_CHARS = import_pipeline_config.item_name_context_max_chars


@step_log("step_1_check_content")
def step_1_check_content(state):
    """
        获取chunks切片和对应file_title
    """
    chunks = state['chunks']
    file_title = state['file_title']
    md_path = state['md_path']
    if not chunks:
        logger.error(f"chunks没有内容,无法继续业务!")
        raise ValueError("chunks没有内容,无法继续业务!")
    if not file_title:
        logger.warning("没有在state读取到file_title,计划给与默认值!")
        if md_path:
            file_title = Path(md_path).stem
        if not file_title:
            file_title = "default"
        state['file_title'] = file_title
    return chunks, file_title


@step_log("step_2_build_context")
def step_2_build_context(chunks) -> str:
    """
       根据切片内容,拼接context,提供给模型进行识别item_name
    """
    # 1. 截取topk
    current_chunks = chunks[:DEFAULT_ITEM_NAME_CHUNK_K]
    chunks_str_list = []
    # 2. 循环处理
    for index, item in enumerate(current_chunks, start=1):
        title = item.get("title", "")
        content = item.get("content", "")
        chunks_str_list.append(f"切片:{index},标题:{title},内容:{content}")

    # 4. join
    chunks_str = "\n".join(chunks_str_list)
    # 5. 检查最大的长度
    final_context = chunks_str[:CONTEXT_TOTAL_MAX_CHARS]
    return final_context


@step_log("step_3_call_llm")
def step_3_call_llm(context, file_title) -> str:
    """
          调用 llm , 总结和获取 item_name,如果没有返回! 使用file_title
    """
    # 1. 获取模型对象
    llm = get_llm_client()
    # 2. 拼接提示词
    system_prompt_str = load_prompt("product_recognition_system")
    user_prompt_str = load_prompt("item_name_recognition", file_title=file_title, context=context)

    messages = [
        SystemMessage(content=system_prompt_str),
        HumanMessage(content=user_prompt_str)
    ]
    # 3. 链式组装
    chain = llm | StrOutputParser()
    # 4. 调用并获取结果
    item_name = chain.invoke(messages)
    # 5. 结果校验
    if not item_name:
        item_name = file_title
    return item_name


@step_log("step_4_update_chunks_and_state")
def step_4_update_chunks_and_state(state, item_name, chunks):
    """
        修改state和chunks
    """
    state['item_name'] = item_name
    for chunk in chunks:
        chunk['item_name'] = item_name
    state['chunks'] = chunks


@step_log("step_5_generate_embeddings")
def step_5_generate_embeddings(item_name):
    """
    根据生成向量 -> 稠密 + 稀疏
    """
    vectors = generate_embeddings([item_name])  # 单文本需封装为列表
    dense_vector = vectors['dense'][0]
    sparse_vector = vectors['sparse'][0]
    return dense_vector, sparse_vector


@step_log("step_6_insert_milvus")
def step_6_insert_milvus(item_name, file_title, dense_vector, sparse_vector):
    """
        将四个参数插入到对应milvus中即可
    """
    # 1. 链接milvus的客户端
    milvus_client = get_milvus_client()
    # 2. 创建表对应schema
    if not milvus_client.has_collection(collection_name=milvus_config.item_name_collection):
        schema = milvus_client.create_schema(
            auto_id=True,
            enable_dynamic_field=True,
        )
        schema.add_field(field_name="pk", datatype=DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=512)
        schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=512)
        schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=1024)
        schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)

        # 3. 创建列对应的索引
        index_params = milvus_client.prepare_index_params()
        index_params.add_index(
            field_name="dense_vector",
            index_name="dense_vector_index",
            index_type="AUTOINDEX",
            metric_type="IP"
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_vector_index",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
            params={"inverted_index_algo": "DAAT_MAXSCORE"}
        )

        # 4. 创建集合collection
        milvus_client.create_collection(
            collection_name=milvus_config.item_name_collection,
            schema=schema,
            index_params=index_params,
        )

    # 5. 先删除之前存在的item_name
    milvus_client.delete(
        collection_name=milvus_config.item_name_collection,
        filter=f"item_name == '{item_name}'"
    )
    milvus_client.load_collection(collection_name=milvus_config.item_name_collection)

    # 6. 向集合插入最新的item_name数据
    data = [
        {
            "file_title": file_title,
            "item_name": item_name,
            "dense_vector": dense_vector,
            "sparse_vector": sparse_vector
        }
    ]
    milvus_client.insert(collection_name=milvus_config.item_name_collection, data=data)


@node_log("node_item_name_recognition")
def node_item_name_recognition(state: ImportGraphState) -> ImportGraphState:
    """
    节点: 主体识别 (node_item_name_recognition)
    为什么叫这个名字: 识别文档核心描述的物品/商品名称 (Item Name)。
    """
    #1. 日志和任务添加
    add_running_task(state['task_id'], sys._getframe().f_code.co_name, state.get("is_stream", False))
    #2. 检查chunks和file_title
    chunks, file_title = step_1_check_content(state)
    #3. chunks拼接了提示词的上下文
    context = step_2_build_context(chunks)
    #4. 调用模型获取item_name
    item_name = step_3_call_llm(context, file_title)
    #5. 修改state和chunks
    step_4_update_chunks_and_state(state, item_name, chunks)
    #6. item_name生成稠密和稀疏向量
    dense_vector, sparse_vector = step_5_generate_embeddings(item_name)
    #7. 准备集合和插入数据milvus中
    step_6_insert_milvus(item_name, file_title, dense_vector, sparse_vector)
    #8. 任务和日志处理
    add_done_task(state['task_id'], sys._getframe().f_code.co_name, state.get("is_stream", False))
    return state


if __name__ == "__main__":
    # 本地测试入口
    logger.info("=== 开始执行商品名称识别节点本地测试 ===")
    try:
        mock_state = ImportGraphState({
            "task_id": "test_task_123456",
            "file_title": "华为Mate60 Pro手机使用说明书",
            "md_path": "华为Mate60Pro说明书",
            "chunks": [
                {
                    "title": "产品简介",
                    "content": "华为Mate60 Pro是华为公司2023年发布的旗舰智能手机，搭载麒麟9000S芯片，支持卫星通话功能。"
                },
                {
                    "title": "电池参数",
                    "content": "电池容量5000mAh，支持88W有线超级快充。"
                }
            ]
        })
        result_state = node_item_name_recognition(mock_state)
        logger.info("=== 商品名称识别节点本地测试完成 ===")
        logger.info(f"最终识别商品名称：{result_state.get('item_name')}")
        logger.info(f"切片数量：{len(result_state.get('chunks', []))}")
    except Exception as e:
        logger.exception(f"商品名称识别节点本地测试失败，原因：{str(e)}")
