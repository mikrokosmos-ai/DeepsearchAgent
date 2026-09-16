# 加载环境变量：从 .env 文件读取配置
from dotenv import load_dotenv
# 导入LangGraph核心依赖
from langgraph.graph import StateGraph, END

from app.core.logger import logger
from app.pipelines.import_pipeline.state import ImportGraphState
from app.pipelines.import_pipeline.nodes.node_entry import node_entry
from app.pipelines.import_pipeline.nodes.node_pdf_to_md import node_pdf_to_md
from app.pipelines.import_pipeline.nodes.node_md_img import node_md_img
from app.pipelines.import_pipeline.nodes.node_document_split import node_document_split
from app.pipelines.import_pipeline.nodes.node_item_name_recognition import node_item_name_recognition
from app.pipelines.import_pipeline.nodes.node_bge_embedding import node_bge_embedding
from app.pipelines.import_pipeline.nodes.node_import_milvus import node_import_milvus
from app.pipelines.import_pipeline.nodes.node_import_kg import node_import_kg


# 初始化环境变量：必须在配置读取前执行
load_dotenv()

# 1. 定义状态图对象,并且指定全局state类型
workflow = StateGraph(ImportGraphState)
# 2. 添加节点
workflow.add_node("node_entry", node_entry)
workflow.add_node("node_pdf_to_md", node_pdf_to_md)
workflow.add_node("node_md_img", node_md_img)
workflow.add_node("node_document_split", node_document_split)
workflow.add_node("node_item_name_recognition", node_item_name_recognition)
workflow.add_node("node_bge_embedding", node_bge_embedding)
workflow.add_node("node_import_milvus", node_import_milvus)
workflow.add_node("node_import_kg", node_import_kg)

# 3. 指定入口节点
workflow.set_entry_point("node_entry")


# 4. 入口节点后的条件边
def after_entry_node(state: ImportGraphState):
    if state['is_md_read_enabled']:
        return "node_md_img"
    elif state['is_pdf_read_enabled']:
        return "node_pdf_to_md"
    else:
        return END


workflow.add_conditional_edges(
    "node_entry", after_entry_node,
    {
        "node_md_img": "node_md_img",
        "node_pdf_to_md": "node_pdf_to_md",
        END: END
    })
# 5. 设置静态条件边
workflow.add_edge("node_pdf_to_md", "node_md_img")
workflow.add_edge("node_md_img", "node_document_split")
workflow.add_edge("node_document_split", "node_item_name_recognition")
workflow.add_edge("node_item_name_recognition", "node_bge_embedding")
workflow.add_edge("node_bge_embedding", "node_import_milvus")
# 6. 末端线性尾插：Milvus 入库完成后继续抽取知识图谱
workflow.add_edge("node_import_milvus", "node_import_kg")
workflow.add_edge("node_import_kg", END)

# 7. 编译图对象即可
kb_import_app = workflow.compile()


if __name__ == "__main__":
    from app.core.paths import PROJECT_ROOT
    import os

    # 全流程测试
    logger.info("===== 开始执行知识图谱导入全流程测试 =====")
    test_pdf_name = os.path.join("doc", "hak180产品安全手册.pdf")
    test_pdf_path = os.path.join(PROJECT_ROOT, test_pdf_name)
    test_output_dir = os.path.join(PROJECT_ROOT, "output")
    os.makedirs(test_output_dir, exist_ok=True)

    if not os.path.exists(test_pdf_path):
        logger.error(f"全流程测试失败：测试PDF文件不存在，路径：{test_pdf_path}")
    else:
        test_state = ImportGraphState({
            "task_id": "test_kg_import_workflow_001",
            "local_file_path": test_pdf_path,
            "local_dir": test_output_dir,
            "is_pdf_read_enabled": False,
            "is_md_read_enabled": False,
            "is_stream": False
        })
        try:
            final_state = None
            for step in kb_import_app.stream(test_state, stream_mode="values"):
                current_node = list(step.keys())[-1] if step else "未知节点"
                logger.info(f"✅ 节点执行完成：{current_node}")
                final_state = step

            if final_state:
                logger.info("-" * 80)
                logger.info("===== 全流程测试执行成功，核心结果预览 =====")
                chunks = final_state.get("chunks", [])
                chunk_count = len(chunks)
                md_content = final_state.get("md_content", "")[:150]
                has_embedding = all("dense_vector" in c and "sparse_vector" in c for c in chunks) if chunks else False
                has_chunk_id = all("chunk_id" in c for c in chunks) if chunks else False
                logger.info(f"📄 PDF转MD内容预览（前150字符）：{md_content}...")
                logger.info(f"📝 文档切分总切片数：{chunk_count}")
                logger.info(f"🔍 所有切片是否完成向量化：{'是' if has_embedding else '否'}")
                logger.info(f"🗄️  所有切片是否完成Milvus入库（含chunk_id）：{'是' if has_chunk_id else '否'}")
                logger.info(f"📂 最终状态包含的核心键：{list(final_state.keys())}")
                logger.info("-" * 80)
        except Exception as e:
            logger.exception(f"===== 全流程测试运行失败 =====")
    logger.info("===== 知识图谱导入全流程测试结束 =====")
