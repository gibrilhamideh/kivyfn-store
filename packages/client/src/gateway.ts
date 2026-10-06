import type { JsonObject } from "./internal.js";

/** Application-provided request context; never forwarded to the StoreHost. */
export interface GatewayAuthContext {
  readonly remoteAddress?: string;
  readonly origin?: string;
  readonly headers: Readonly<
    Record<string, string | readonly string[] | undefined>
  >;
}
/**
 * Integration contract for a gateway's own identity and authorization policy.
 * These hooks are types only; the library does not supply an auth service.
 * Authenticate a physical socket, then authorize its requested store binding.
 */
export interface GatewayPolicy<StorePrincipal, UserPrincipal> {
  authenticateStore(
    credentials: JsonObject,
    context: GatewayAuthContext,
  ): Promise<StorePrincipal>;
  authorizeStore(principal: StorePrincipal, storeId: string): Promise<boolean>;
  authenticateUser(
    credentials: JsonObject,
    context: GatewayAuthContext,
  ): Promise<UserPrincipal>;
  authorizeUser(principal: UserPrincipal, storeId: string): Promise<boolean>;
}
