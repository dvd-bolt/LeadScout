import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../http/api";
import type { Operation } from "../types/api";
import { Message } from "./index";

function isPolling(status?: Operation["status"]) {
  return status === "PENDING" || status === "RUNNING";
}

export function operationNeedsInput(operation?: Operation) {
  return operation?.status === "NEEDS_INPUT" || operation?.result?.status === "NEEDS_FIELDS";
}

export function operationSucceeded(operation?: Operation) {
  if (!operation || operation.status !== "SUCCEEDED" || operationNeedsInput(operation)) return false;
  const resultStatus = operation.result?.status;
  return resultStatus !== "ERROR" && resultStatus !== "FAILED";
}

export function OperationStatus({
  operationId,
  scopeKey,
  onTerminal,
  onError,
}: {
  operationId: string | null;
  scopeKey: number | string | null | undefined;
  onTerminal?: (operation: Operation) => void;
  onError?: (error: Error) => void;
}) {
  const reported = useRef<string | null>(null);
  const terminalCallback = useRef(onTerminal);
  terminalCallback.current = onTerminal;
  const errorCallback = useRef(onError);
  errorCallback.current = onError;
  const query = useQuery({
    queryKey: ["operation", scopeKey ?? "none", operationId],
    queryFn: () => api.operation(operationId!),
    enabled: Boolean(operationId),
    refetchInterval: (result) => isPolling(result.state.data?.status) ? 3000 : false,
  });
  const state = query.data;
  const needsInput = operationNeedsInput(state);

  useEffect(() => {
    if (!state || isPolling(state.status)) return;
    const reportKey = `${state.id}:${state.status}:${needsInput ? "needs-input" : "terminal"}`;
    if (reported.current === reportKey) return;
    reported.current = reportKey;
    terminalCallback.current?.(state);
  }, [needsInput, state]);

  useEffect(() => {
    if (!query.error || reported.current === `error:${operationId}`) return;
    reported.current = `error:${operationId}`;
    errorCallback.current?.(query.error);
  }, [operationId, query.error]);

  if (query.isError) return <Message notice={{ text: query.error.message, error: true }} />;
  if (!operationId || !state) return null;
  if (needsInput) {
    const message = typeof state.result.message === "string" ? state.result.message : "Нужно дополнить данные резюме.";
    return <Message notice={{ text: message }} />;
  }
  if (state.status === "FAILED") return <Message notice={{ text: state.error_text || "Операция завершилась с ошибкой", error: true }} />;
  if (state.result?.status === "ERROR" || state.result?.status === "FAILED") {
    return <Message notice={{ text: typeof state.result.message === "string" ? state.result.message : "Операция не выполнена", error: true }} />;
  }
  if (state.status === "SUCCEEDED") {
    return <Message notice={{ text: typeof state.result.message === "string" ? state.result.message : "Операция завершена" }} />;
  }
  return <Message notice={{ text: "Операция выполняется…" }} />;
}
