import { ApiError } from "../api/client";
import { BootstrapContractError } from "../api/bootstrap-contracts";
import { ModelAcquisitionContractError } from "../api/model-acquisition-contracts";
import { ModelSelectionContractError } from "../api/model-selection-contracts";
import { ModelSourceContractError } from "../api/model-source-contracts";
import { PasswordPolicyContractError } from "../components";
import { NavigationContractError } from "./navigation";

export class AppError extends Error {
  readonly code: string;
  readonly requestId: string | undefined;

  constructor(code: string, requestId?: string) {
    super(code);
    this.name = "AppError";
    this.code = code;
    this.requestId = requestId;
  }
}

export function toAppError(error: unknown): AppError {
  if (error instanceof AppError) {
    return error;
  }
  if (error instanceof ApiError) {
    return new AppError(error.code, error.requestId);
  }
  if (
    error instanceof BootstrapContractError ||
    error instanceof ModelAcquisitionContractError ||
    error instanceof ModelSelectionContractError ||
    error instanceof ModelSourceContractError ||
    error instanceof NavigationContractError ||
    error instanceof PasswordPolicyContractError
  ) {
    return new AppError(error.code);
  }
  return new AppError("interface_error");
}

export function toThrowableError(error: unknown): Error {
  if (
    error instanceof ApiError ||
    error instanceof BootstrapContractError ||
    error instanceof ModelAcquisitionContractError ||
    error instanceof ModelSelectionContractError ||
    error instanceof ModelSourceContractError ||
    error instanceof NavigationContractError ||
    error instanceof PasswordPolicyContractError
  ) {
    return error;
  }
  return toAppError(error);
}
