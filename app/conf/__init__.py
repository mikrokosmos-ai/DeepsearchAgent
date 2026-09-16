"""
配置层

集中承载"连接信息"与"业务可调参数"两类配置，统一从 .env 读取并以模块级
单例对象暴露，供客户端层与流水线节点消费。

分层约定：
    - 连接类配置（milvus / mongo / neo4j / minio / mineru / lm / bailian_mcp）
      只描述"去哪里连、用什么凭据"，不含业务阈值；
    - 模型类配置（embedding / reranker）描述本地权重路径与推理设备；
    - 链路可调参数（import_pipeline_config / query_pipeline_config）描述业务
      阈值，且在模块加载时执行 fail-fast 不变式校验。

导入本包的任意子模块即会触发该模块的单例构建与校验（fail-fast），
因此配置错误会在进程启动阶段直接暴露，不会拖到运行期才失败。
"""
