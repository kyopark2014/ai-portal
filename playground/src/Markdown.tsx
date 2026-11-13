import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// 모델 출력은 신뢰할 수 없는 입력이다. 원시 HTML 은 렌더링하지 않고(react-markdown 기본), 링크는 http(s)/mailto 만 허용하며 새 탭에서 연다.
// 6F~12F 처럼 범위에 쓰는 단일 ~ 는 취소선으로 바꾸지 않는다(cde-pilot 과 같음).
const SAFE = /^(https?:|mailto:)/i;

export default function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
      urlTransform={(url) => (SAFE.test(url) ? url : "")}
      components={{
        a: ({ node: _n, ...p }) => <a {...p} target="_blank" rel="noopener noreferrer nofollow" />,
        img: () => null,   // 외부 이미지 로딩(추적/유출)은 막는다
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
