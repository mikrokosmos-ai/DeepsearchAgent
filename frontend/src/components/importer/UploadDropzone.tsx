/**
 * 知识导入上传区
 *
 * 用 `beforeUpload` 回调（每个文件触发一次）收集文件并阻止 antd 自动上传，
 * 避免 `onChange` 在多次选择时把旧文件重复提交。
 */
import { InboxOutlined } from "@ant-design/icons";
import { Upload } from "antd";

const { Dragger } = Upload;

interface UploadDropzoneProps {
  disabled?: boolean;
  onFiles: (files: File[]) => void;
}

export function UploadDropzone({ disabled, onFiles }: UploadDropzoneProps) {
  return (
    <div className="ui-card">
      <div className="admin-table-head">
        <div>
          <h3 className="ui-card-title">上传知识文档</h3>
          <p className="ui-card-description">
            单个文件不超过 100MB；上传后自动完成解析、图片理解、切分、向量化入库与知识图谱抽取
          </p>
        </div>
      </div>
      <div className="ui-card-body" style={{ paddingTop: 0 }}>
        <Dragger
          accept=".pdf,.md,.markdown"
          className="admin-dropzone"
          disabled={disabled}
          multiple
          showUploadList={false}
          beforeUpload={(file) => {
            onFiles([file]);
            // 阻止 antd 自动上传：由 useKbImport 统一走 /api/kb/import
            return false;
          }}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">拖拽或点击选择 PDF / Markdown 文件</p>
          <p className="ant-upload-hint">可多选；提交后每个文件生成一条独立导入任务</p>
        </Dragger>
      </div>
    </div>
  );
}
