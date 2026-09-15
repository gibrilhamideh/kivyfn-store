import { format } from "../model";

export function Inspector({ value }: { value: unknown }) {
  return <pre className="inspector">{format(value)}</pre>;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}
