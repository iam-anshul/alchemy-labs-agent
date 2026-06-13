interface AsyncStateProps {
  title: string;
  detail?: string;
  actionLabel?: string;
  onAction?: () => void;
}

export function AsyncState({
  title,
  detail,
  actionLabel,
  onAction,
}: AsyncStateProps) {
  return (
    <div className="async-state" role={onAction ? "alert" : "status"}>
      <strong>{title}</strong>
      {detail && <p>{detail}</p>}
      {actionLabel && onAction && (
        <button className="button button--outline" type="button" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}
