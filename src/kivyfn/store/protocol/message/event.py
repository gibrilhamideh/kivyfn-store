# src/kivyfn/store/protocol/message/event.py
from __future__ import annotations

from dataclasses import dataclass

from kivyfn.store.events import StoreCommit
from kivyfn.store.query import QueryResult


@dataclass(frozen=True, slots=True)
class QueryEvent:
    """Changes to one live query result at the enclosing commit's revision.

    Rows contain complete values for newly included or changed result rows.
    Removed IDs leave this query result; their Store rows may still exist.
    """

    subscription_id: str
    rows: QueryResult = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventMessage:
    """One commit's subscription notification for one client.

    The Store supplies a stamped commit. Its events are filtered to this
    client's whole-schema subscriptions, preserving their original order.
    Query changes share the commit's operation ID, instance ID, and revision.

    Subscription IDs identify every registration covered by this message,
    including queries. Clients route only to IDs they still have bound,
    stage all changes together, and then publish State notifications.

    An empty filtered commit can carry query changes or advance the covered
    subscriptions through a revision without changing their visible data.
    """

    commit: StoreCommit
    subscription_ids: tuple[str, ...]
    query_events: tuple[QueryEvent, ...] = ()
