import { FileImageOutlined } from "@ant-design/icons";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toProxySrc } from "../lib/imageProxy";

interface MarkdownRendererProps {
  content: string;
}

interface KnowledgeImageProps {
  alt?: string;
  src: string;
  /** 轮次内图片区的缩略图形态 */
  thumb?: boolean;
}

/**
 * 知识库配图：限宽展示 + 点击看大图 + 加载失败占位。
 * 内联 Markdown 图与轮次内图片区共用同一实现，保证两处外观与行为一致。
 */
interface KnowledgeImageFallbackProps {
  alt: string;
  thumb?: boolean;
}

/** 图片加载失败时的占位：图标 + 替代文字，尺寸受控不撑破布局 */
export function KnowledgeImageFallback({ alt, thumb = false }: KnowledgeImageFallbackProps) {
  const className = ["agent-image", thumb ? "agent-image--thumb" : "", "agent-image--failed"]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={className}>
      <FileImageOutlined aria-hidden />
      <span className="agent-image-alt">{alt}</span>
    </span>
  );
}

export function KnowledgeImage({ alt = "知识库配图", src, thumb = false }: KnowledgeImageProps) {
  const [failed, setFailed] = useState(false);
  const [zoomed, setZoomed] = useState(false);
  const className = ["agent-image", thumb ? "agent-image--thumb" : ""].filter(Boolean).join(" ");

  if (failed) {
    return <KnowledgeImageFallback alt={alt} thumb={thumb} />;
  }

  return (
    <>
      <button
        aria-label={`查看大图：${alt}`}
        className={className}
        onClick={() => setZoomed(true)}
        type="button"
      >
        <img alt={alt} onError={() => setFailed(true)} src={src} />
      </button>
      {zoomed ? (
        <div
          aria-label="图片大图预览"
          className="agent-image-zoom"
          onClick={() => setZoomed(false)}
          role="presentation"
        >
          <img alt={alt} src={src} />
          <span className="agent-image-zoom-hint">点击任意处关闭</span>
        </div>
      ) : null}
    </>
  );
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ children, href, ...props }) {
            return (
              <a href={href} rel="noreferrer" target="_blank" {...props}>
                {children}
              </a>
            );
          },
          img({ alt, src }) {
            return src ? <KnowledgeImage alt={alt} src={toProxySrc(src)} /> : null;
          }
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
