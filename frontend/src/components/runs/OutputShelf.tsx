import { FileOutput } from "lucide-react";

import type { RunArtifactEntry } from "../../types/runArtifacts";
import { formatBytes } from "../../utils/format";
import "./OutputShelf.css";

interface OutputShelfProps {
  outputs: RunArtifactEntry[];
  selectedOutputId: string | null;
  onSelectOutput: (output: RunArtifactEntry) => void;
}

export default function OutputShelf({
  outputs,
  selectedOutputId,
  onSelectOutput,
}: OutputShelfProps) {
  if (outputs.length === 0) return null;

  return (
    <section className="output-shelf">
      <header>
        <FileOutput size={13} />
        <strong>Produced files</strong>
        <span>{outputs.length}</span>
      </header>
      <div className="output-shelf__track">
        {outputs.map((output) => (
          <button
            className="output-shelf__item"
            data-active={selectedOutputId === output.id}
            type="button"
            key={output.id}
            onClick={() => onSelectOutput(output)}
          >
            <span>{fileExtension(output.artifact.filename)}</span>
            <strong>{output.artifact.filename ?? "Produced file"}</strong>
            {output.artifact.bytes !== null && (
              <small>{formatBytes(output.artifact.bytes)}</small>
            )}
          </button>
        ))}
      </div>
    </section>
  );
}

function fileExtension(filename: string | null) {
  return filename?.split(".").pop()?.toUpperCase().slice(0, 5) ?? "FILE";
}
