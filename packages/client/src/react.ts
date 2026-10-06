import {
    createContext,
    createElement,
    useContext,
    useEffect,
    useMemo,
} from "react";
import type { ReactNode } from "react";
import { observer } from "mobx-react-lite";
import { StateManager } from "./state.js";
import type { CollectionState, QueryState, State } from "./state.js";
import type {
    CollectionRef,
    QueryRef,
    SingleRef,
    SourceRef,
} from "./schema.js";

export { observer };
const Context = createContext<StateManager | null>(null);

/** Context only. The application owns connection and manager lifetime. */
export function StoreProvider({
    manager,
    children,
}: {
    manager: StateManager;
    children?: ReactNode;
}) {
    return createElement(Context.Provider, { value: manager }, children);
}
export function useStore(): StateManager {
    const manager = useContext(Context);
    if (!manager) throw new Error("useStore must be inside a StoreProvider");
    return manager;
}
export function useStoreState<T extends object, S extends object>(
    source: SingleRef<T, S>,
): State<T, S>;
export function useStoreState<T extends object, S extends object>(
    source: CollectionRef<T, S>,
): CollectionState<T, S>;
export function useStoreState<T extends object>(
    source: QueryRef<T>,
): QueryState<T>;
export function useStoreState(source: SourceRef): unknown {
    const manager = useStore();
    const prepared = useMemo(() => manager.prepare(source), [manager, source]);
    useEffect(() => prepared.retain(), [prepared]);
    return prepared.view;
}
