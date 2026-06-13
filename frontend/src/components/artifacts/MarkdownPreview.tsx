import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import "./MarkdownPreview.css";

interface MarkdownPreviewProps {
  content: string;
  filename?: string | null;
}

export default function MarkdownPreview({
  content,
  filename,
}: MarkdownPreviewProps) {
  const isTodo = filename?.toLowerCase() === "todo.md";

  return (
    <article className={`markdown-preview${isTodo ? " markdown-preview--todo" : ""}`}>
      {isTodo && (
        <header className="markdown-preview__context">
          <span>Run plan</span>
          <strong>Tasks and progress</strong>
        </header>
      )}
      <div className="markdown-preview__content">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            a: ({ href, children }) => (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            ),
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
    </article>
  );
}
