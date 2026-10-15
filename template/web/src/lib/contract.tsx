// The generated operation table, made available to every component by id.
import { createContext, useContext, useMemo, type ReactNode } from "react";
import type { OperationWire } from "./types";

interface ContractValue {
  appName: string;
  operation: (id: string) => OperationWire;
}

const ContractContext = createContext<ContractValue | null>(null);

export function ContractProvider({
  appName,
  operations,
  children,
}: {
  appName: string;
  operations: Record<string, OperationWire>;
  children: ReactNode;
}) {
  const value = useMemo<ContractValue>(
    () => ({
      appName,
      operation: (id) => {
        const op = operations[id];
        if (!op) throw new Error(`Unknown operation "${id}"`);
        return op;
      },
    }),
    [appName, operations],
  );
  return <ContractContext.Provider value={value}>{children}</ContractContext.Provider>;
}

export function useContract(): ContractValue {
  const value = useContext(ContractContext);
  if (!value) throw new Error("useContract must be used inside <ContractProvider>");
  return value;
}
