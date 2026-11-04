import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// 모델 출력은 신뢰할 수 없는 입력이다. react-markdown 은 원시 HTML 을 렌더링하지 않고(기본값),
// 여기서는 링크 스킴도 http(s)/mailto 로 제한하고 새 탭에서 안전하게 연다.
const SAFE = /^(https?:|mailto:)/i;

export default function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => (SAFE.test(url) ? url : "")}
        components={{
          a: ({ node: _n, ...p }) => <a {...p} target="_blank" rel="noopener noreferrer nofollow" />,
          img: () => null,   // 외부 이미지 로딩(추적/유출)은 막는다
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
