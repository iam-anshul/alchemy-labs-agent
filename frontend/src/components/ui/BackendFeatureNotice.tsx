import { LockKeyhole } from "lucide-react";

import "./BackendFeatureNotice.css";

interface BackendFeatureNoticeProps {
  title: string;
  detail: string;
}

export default function BackendFeatureNotice({
  title,
  detail,
}: BackendFeatureNoticeProps) {
  return (
    <div className="backend-feature-notice" aria-disabled="true">
      <span className="backend-feature-notice__icon" aria-hidden="true">
        <LockKeyhole size={16} />
      </span>
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
      <span className="backend-feature-notice__badge">Backend API required</span>
    </div>
  );
}
