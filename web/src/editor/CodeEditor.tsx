import { python } from "@codemirror/lang-python";
import CodeMirror from "@uiw/react-codemirror";
import type { ReactElement } from "react";

export default function CodeEditor({ value, onChange }: { value: string; onChange: (value: string) => void }): ReactElement {
  return <CodeMirror value={value} height="260px" extensions={[python()]} theme="dark" onChange={onChange} aria-label="策略 Python 代码" />;
}
