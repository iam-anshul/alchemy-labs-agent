import { ArrowRight } from "lucide-react";
import { useState } from "react";

import type { PendingQuestion } from "../../types/events";

interface QuestionCardProps {
  question: PendingQuestion;
  isSubmitting: boolean;
  onSubmit: (answer: string) => Promise<void>;
}

export default function QuestionCard({
  question,
  isSubmitting,
  onSubmit,
}: QuestionCardProps) {
  const [selectedOption, setSelectedOption] = useState<number | null>(null);
  const [textAnswer, setTextAnswer] = useState("");

  const answer = question.kind === "mcq"
    ? selectedOption === null ? "" : question.options[selectedOption] ?? ""
    : textAnswer.trim();

  return (
    <section className="question-card" aria-labelledby="question-title">
      <p className="question-card__eyebrow">Run paused · your input is needed</p>
      <h3 id="question-title">{question.question}</h3>

      {question.kind === "mcq" ? (
        <div className="question-options">
          {question.options.map((option, index) => (
            <button
              className="question-option"
              data-selected={selectedOption === index}
              type="button"
              key={option}
              onClick={() => setSelectedOption(index)}
            >
              <span>{String.fromCharCode(65 + index)}</span>
              <strong>{option}</strong>
              {question.recommendedOption === index && <em>recommended</em>}
              <i aria-hidden="true" />
            </button>
          ))}
        </div>
      ) : (
        <textarea
          className="question-card__input"
          rows={3}
          value={textAnswer}
          placeholder="Type your answer..."
          onChange={(event) => setTextAnswer(event.target.value)}
        />
      )}

      <footer className="question-card__footer">
        <span>The run continues after you answer.</span>
        <button
          className="button button--primary"
          type="button"
          disabled={!answer || isSubmitting}
          onClick={() => void onSubmit(answer)}
        >
          {isSubmitting ? "Sending..." : "Send answer"}
          <ArrowRight size={13} />
        </button>
      </footer>
    </section>
  );
}
