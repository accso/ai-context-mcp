import { Injectable } from "@angular/core";

// Fixture only — exercises tree-sitter chunk boundaries (_chunk_by_treesitter),
// the Angular decorator-attachment fix (_extend_start_to_decorator, which
// should keep this @Injectable() attached to the class chunk below it, not
// split into its own preamble-adjacent chunk), and dispatch-arm sub-chunking
// (_chunk_dispatch_arms — the switch below has 3 arms, meeting
// _MIN_ARMS_FOR_SUBCHUNK).
@Injectable()
export class NotificationService {

  route(eventType: string, payload: unknown): string {
    switch (eventType) {
      case "order.created":
        return this.handleOrderCreated(payload);
      case "order.cancelled":
        return this.handleOrderCancelled(payload);
      case "user.registered":
        return this.handleUserRegistered(payload);
      default:
        return "unhandled";
    }
  }

  private handleOrderCreated(payload: unknown): string {
    return `order created: ${JSON.stringify(payload)}`;
  }

  private handleOrderCancelled(payload: unknown): string {
    return `order cancelled: ${JSON.stringify(payload)}`;
  }

  private handleUserRegistered(payload: unknown): string {
    return `user registered: ${JSON.stringify(payload)}`;
  }
}
