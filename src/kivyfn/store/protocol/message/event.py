# src/kivyfn/store/protocol/message/event.py
from __future__ import annotations

from dataclasses import dataclass

from kivyfn.store.events import StoreCommit
from kivyfn.store.query import QueryResult


@dataclass(frozen=True, slots=True)
class QueryEvent:
    """Changes to one live query result caused by the enclosing commit.

    Rows contain complete values for newly included or changed result rows.
    Removed IDs leave this query result; their Store rows may still exist.
    The enclosing commit supplies the Store instance ID, operation ID, and
    revisions of changed dependency schemas. No query counter is needed.
    """

    subscription_id: str
    rows: QueryResult = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventMessage:
    """One commit's subscription notification for one client.

    The Store supplies a stamped commit. Host filters its events to this
    client's covered whole-schema subscriptions, preserving event order.
    schema_revisions includes changed schemas needed by any covered
    subscription, including dependencies of covered queries. It can contain
    schemas whose raw events have been filtered out.

    Subscription IDs identify every registration covered by this message,
    including queries. Clients route only to IDs they still have bound,
    stage all changes together, and then publish State notifications.

    A covered query with unchanged result rows needs no QueryEvent; its
    dependency revisions still advance. An empty filtered event tuple can
    therefore carry query changes or just changed dependency revisions.

    Revision progress belongs to each covered subscription. Query dependency
    metadata alone must not advance a whole-schema cache that did not receive
    its canonical events. Host filters revision metadata to the remaining
    covered subscriptions when queued delivery is prepared.
    """

    commit: StoreCommit
    subscription_ids: tuple[str, ...]
    query_events: tuple[QueryEvent, ...] = ()
