import {
  useId,
  useRef,
  type InputHTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from "react";

import { Icon } from "./icon";

export interface TextFieldProps extends Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "size"
> {
  controlSize?: "m" | "l";
  errorText?: string;
  helpText?: string;
  label: string;
  trailingAction?: ReactNode;
}

export function TextField({
  className,
  controlSize = "m",
  errorText,
  helpText,
  id,
  label,
  required,
  trailingAction,
  ...inputProps
}: TextFieldProps) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const helpId = `${inputId}-help`;
  const errorId = `${inputId}-error`;
  const inputRef = useRef<HTMLInputElement>(null);
  const describedBy = [
    inputProps["aria-describedby"],
    errorText ? errorId : undefined,
    helpText ? helpId : undefined,
  ]
    .filter(Boolean)
    .join(" ");

  function focusInput(event: MouseEvent<HTMLDivElement>) {
    if (event.target === event.currentTarget) {
      event.preventDefault();
      inputRef.current?.focus();
    }
  }

  return (
    <div className="pw-field">
      <label className="pw-field__label" htmlFor={inputId}>
        {label}
        {required ? (
          <span aria-hidden="true" className="pw-field__required">
            必填
          </span>
        ) : null}
      </label>
      <div
        className="pw-input-target"
        data-invalid={errorText !== undefined || undefined}
        onMouseDown={focusInput}
      >
        <input
          {...inputProps}
          aria-describedby={describedBy || undefined}
          aria-invalid={errorText !== undefined || undefined}
          className={[
            "pw-input",
            controlSize === "l" ? "pw-input--l" : undefined,
            className,
          ]
            .filter(Boolean)
            .join(" ")}
          id={inputId}
          ref={inputRef}
          required={required}
        />
        {trailingAction === undefined ? null : (
          <span className="pw-field__trailing">{trailingAction}</span>
        )}
      </div>
      {helpText ? (
        <span className="pw-field__help" id={helpId}>
          {helpText}
        </span>
      ) : null}
      {errorText ? (
        <span className="pw-field__error" id={errorId} role="alert">
          <Icon name="error" size="xs" />
          {errorText}
        </span>
      ) : null}
    </div>
  );
}
