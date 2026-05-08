from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
import random

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.database import execute_query, execute_transaction, fetch_all, fetch_one

app = FastAPI(title="Restway Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
VALID_ORDER_STATUSES = {"pending", "preparing", "ready", "delivered", "paid", "cancelled"}
VALID_WAITER_CALL_TYPES = {"general_help", "order_help", "payment"}
VALID_WAITER_CALL_STATUSES = {"pending", "seen", "completed"}


def generate_delivery_pin() -> str:
    return f"{random.randint(1000, 9999)}"


class WaiterCallCreateRequest(BaseModel):
    request_type: Literal["general_help", "order_help", "payment"]

class OrderStatusUpdateRequest(BaseModel):
    new_status: Literal["preparing", "ready"]
    changed_by_staff_id: int | None = None
    note: str | None = None


class DeliverOrderRequest(BaseModel):
    waiter_id: int
    delivery_pin: str


class PayOrderRequest(BaseModel):
    waiter_id: int
    pin: str
    payment_method: Literal["card", "cash"] = "card"

class MoveTableRequest(BaseModel):
    from_table_id: int
    to_table_id: int
    waiter_id: int

class WaiterCallActionRequest(BaseModel):
    staff_id: int

class OrderItemCreate(BaseModel):
    menu_item_id: int
    quantity: int = Field(..., gt=0)


class OrderCreateRequest(BaseModel):
    table_id: int
    created_by_type: Literal["customer", "waiter"]
    created_by_staff_id: int | None = None
    order_type: Literal["initial", "additional"] = "initial"
    items: list[OrderItemCreate]


@app.get("/api/health")
def health_check():
    result = fetch_one("SELECT 1 AS ok;")
    return {
        "success": True,
        "message": "Backend is running",
        "database": result,
    }


@app.get("/api/menu-items")
def get_menu_items():
    query = """
        SELECT
            mi.id,
            mi.name,
            mi.description,
            mi.price,
            mi.image_url,
            (
                mi.is_available = TRUE
                AND NOT EXISTS (
                    SELECT 1
                    FROM menu_item_ingredients mii
                    JOIN ingredients i
                        ON i.id = mii.ingredient_id
                    WHERE mii.menu_item_id = mi.id
                    AND i.stock_quantity < mii.quantity_needed
                )
            ) AS is_available,
            c.name AS category_name,
            COALESCE(ROUND(AVG(mir.rating)::numeric, 1), 0.0) AS average_rating,
            COUNT(mir.id) AS review_count
        FROM menu_items mi
        JOIN categories c
            ON c.id = mi.category_id
        LEFT JOIN menu_item_reviews mir
            ON mir.menu_item_id = mi.id
        GROUP BY
            mi.id,
            mi.name,
            mi.description,
            mi.price,
            mi.image_url,
            mi.is_available,
            c.name
        ORDER BY c.name, mi.name;
    """
    items = fetch_all(query)
    return {
        "success": True,
        "count": len(items),
        "data": items,
    }

class MenuItemReviewCreateRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = None

class IngredientCreateRequest(BaseModel):
    name: str
    stock_quantity: float = Field(..., ge=0)
    unit: str

@app.get("/api/menu-items/{menu_item_id}/reviews")
def get_menu_item_reviews(menu_item_id: int):
    query = """
        SELECT
            id,
            order_id,
            menu_item_id,
            rating,
            comment,
            created_at
        FROM menu_item_reviews
        WHERE menu_item_id = %s
        ORDER BY created_at DESC;
    """
    reviews = fetch_all(query, (menu_item_id,))

    return {
        "success": True,
        "count": len(reviews),
        "data": reviews,
    }


@app.post("/api/orders/{order_id}/items/{menu_item_id}/review")
def create_order_item_review(
    order_id: int,
    menu_item_id: int,
    payload: MenuItemReviewCreateRequest,
):
    order = fetch_one(
        """
        SELECT id, status
        FROM orders
        WHERE id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    if order["status"] != "delivered":
        raise HTTPException(
            status_code=400,
            detail="You can review items only after the order is delivered and before payment.",
        )

    ordered_item = fetch_one(
        """
        SELECT oi.id
        FROM order_items oi
        WHERE oi.order_id = %s
          AND oi.menu_item_id = %s
        LIMIT 1;
        """,
        (order_id, menu_item_id),
    )

    if not ordered_item:
        raise HTTPException(
            status_code=400,
            detail="You can only review items from your own delivered order.",
        )

    existing_review = fetch_one(
        """
        SELECT id
        FROM menu_item_reviews
        WHERE order_id = %s
          AND menu_item_id = %s;
        """,
        (order_id, menu_item_id),
    )

    if existing_review:
        raise HTTPException(
            status_code=400,
            detail="You already reviewed this item for this order.",
        )

    review = fetch_one(
        """
        INSERT INTO menu_item_reviews (
            order_id,
            menu_item_id,
            rating,
            comment
        )
        VALUES (%s, %s, %s, %s)
        RETURNING id, order_id, menu_item_id, rating, comment, created_at;
        """,
        (order_id, menu_item_id, payload.rating, payload.comment),
    )

    return {
        "success": True,
        "message": "Review created successfully.",
        "data": review,
    }
    
@app.post("/api/orders")
def create_order(payload: OrderCreateRequest):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Order must contain at least one item.")

    if payload.created_by_type == "waiter" and payload.created_by_staff_id is None:
        raise HTTPException(
            status_code=400,
            detail="created_by_staff_id is required when created_by_type is 'waiter'.",
        )

    if payload.created_by_type == "customer" and payload.created_by_staff_id is not None:
        raise HTTPException(
            status_code=400,
            detail="created_by_staff_id must be null when created_by_type is 'customer'.",
        )

    table = fetch_one(
        """
        SELECT id, table_number, status
        FROM restaurant_tables
        WHERE id = %s;
        """,
        (payload.table_id,),
    )

    if not table:
        raise HTTPException(status_code=404, detail="Table not found.")

    if payload.created_by_staff_id is not None:
        staff_user = fetch_one(
            """
            SELECT id, full_name, role, is_active
            FROM staff_users
            WHERE id = %s;
            """,
            (payload.created_by_staff_id,),
        )
        if not staff_user:
            raise HTTPException(status_code=404, detail="Staff user not found.")
        if not staff_user["is_active"]:
            raise HTTPException(status_code=400, detail="Staff user is not active.")

    menu_item_ids = [item.menu_item_id for item in payload.items]
    placeholders = ",".join(["%s"] * len(menu_item_ids))

    menu_items = fetch_all(
        f"""
        SELECT id, name, price, is_available
        FROM menu_items
        WHERE id IN ({placeholders});
        """,
        tuple(menu_item_ids),
    )

    menu_item_map = {item["id"]: item for item in menu_items}

    for item in payload.items:
        menu_item = menu_item_map.get(item.menu_item_id)
        if not menu_item:
            raise HTTPException(
                status_code=404,
                detail=f"Menu item with id {item.menu_item_id} not found.",
            )
        if not menu_item["is_available"]:
            raise HTTPException(
                status_code=400,
                detail=f"Menu item '{menu_item['name']}' is not available.",
            )

    def transaction_logic(conn, cur):
        cur.execute(
            """
            SELECT id
            FROM table_sessions
            WHERE table_id = %s AND status = 'active'
            ORDER BY started_at DESC
            LIMIT 1;
            """,
            (payload.table_id,),
        )
        active_session = cur.fetchone()

        if active_session:
            session_id = active_session["id"]
        else:
            cur.execute(
                """
                INSERT INTO table_sessions (table_id, status)
                VALUES (%s, 'active')
                RETURNING id;
                """,
                (payload.table_id,),
            )
            session_id = cur.fetchone()["id"]
        delivery_pin = generate_delivery_pin()
        total_amount = Decimal("0.00")
        order_lines: list[dict] = []

        for item in payload.items:
            menu_item = menu_item_map[item.menu_item_id]
            unit_price = Decimal(str(menu_item["price"]))
            line_total = unit_price * item.quantity
            total_amount += line_total

            order_lines.append(
                {
                    "menu_item_id": item.menu_item_id,
                    "quantity": item.quantity,
                    "unit_price": unit_price,
                    "line_total": line_total,
                }
            )

        order_number = f"ORD-{payload.table_id}-{session_id}-{int(datetime.now().timestamp())}"

        cur.execute(
            """
            INSERT INTO orders (
                session_id,
                table_id,
                order_number,
                order_type,
                created_by_type,
                created_by_staff_id,
                status,
                total_amount,
                cancel_deadline,
                delivery_pin
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                'pending',
                %s,
                CURRENT_TIMESTAMP + INTERVAL '5 minutes',
                %s
            )
            RETURNING id, created_at, cancel_deadline, delivery_pin;
            """,
            (
                session_id,
                payload.table_id,
                order_number,
                payload.order_type,
                payload.created_by_type,
                payload.created_by_staff_id,
                total_amount,
                delivery_pin,
            ),
        )

        created_order = cur.fetchone()
        order_id = created_order["id"]

        for line in order_lines:
            cur.execute(
                """
                INSERT INTO order_items (
                    order_id,
                    menu_item_id,
                    quantity,
                    unit_price,
                    line_total,
                    item_status
                )
                VALUES (%s, %s, %s, %s, %s, 'pending');
                """,
                (
                    order_id,
                    line["menu_item_id"],
                    line["quantity"],
                    line["unit_price"],
                    line["line_total"],
                ),
            )

        cur.execute(
            """
            INSERT INTO order_status_logs (
                order_id,
                old_status,
                new_status,
                changed_by_staff_id,
                note
            )
            VALUES (%s, NULL, 'pending', %s, %s);
            """,
            (
                order_id,
                payload.created_by_staff_id,
                "Order created",
            ),
        )

        cur.execute(
            """
            UPDATE restaurant_tables
            SET status = 'occupied'
            WHERE id = %s;
            """,
            (payload.table_id,),
        )

        cur.execute(
            """
            SELECT
                oi.id,
                oi.menu_item_id,
                mi.name AS menu_item_name,
                oi.quantity,
                oi.unit_price,
                oi.line_total,
                oi.item_status
            FROM order_items oi
            JOIN menu_items mi
                ON mi.id = oi.menu_item_id
            WHERE oi.order_id = %s
            ORDER BY oi.id;
            """,
            (order_id,),
        )
        created_items = cur.fetchall()

        return {
            "order_id": order_id,
            "session_id": session_id,
            "table_id": payload.table_id,
            "order_type": payload.order_type,
            "created_by_type": payload.created_by_type,
            "created_by_staff_id": payload.created_by_staff_id,
            "status": "pending",
            "total_amount": float(total_amount),
            "created_at": created_order["created_at"],
            "cancel_deadline": created_order["cancel_deadline"],
            "delivery_pin": created_order["delivery_pin"],
            "delivery_pin_verified_at": None,
            "items": created_items,
        }

    result = execute_transaction(transaction_logic)

    return {
        "success": True,
        "message": "Order created successfully.",
        "data": result,
    }


@app.get("/api/tables/{table_id}/active-session")
def get_active_table_session(table_id: int):
    query = """
        SELECT
            ts.id AS session_id,
            ts.table_id,
            ts.started_at,
            ts.ended_at,
            ts.status
        FROM table_sessions ts
        WHERE ts.table_id = %s
          AND ts.status = 'active'
        ORDER BY ts.started_at DESC
        LIMIT 1;
    """
    session = fetch_one(query, (table_id,))
    if not session:
        raise HTTPException(status_code=404, detail="No active session found for this table.")
    return {
        "success": True,
        "data": session,
    }


@app.get("/api/sessions/{session_id}/orders")
def get_session_orders(session_id: int):
    query = """
        SELECT
            o.id,
            o.order_number,
            o.order_type,
            o.created_by_type,
            o.created_by_staff_id,
            o.status,
            o.total_amount,
            o.cancel_deadline,
            o.created_at,
            o.updated_at
        FROM orders o
        WHERE o.session_id = %s
        ORDER BY o.created_at DESC;
    """
    orders = fetch_all(query, (session_id,))
    return {
        "success": True,
        "count": len(orders),
        "data": orders,
    }

@app.get("/api/kitchen/orders")
def get_kitchen_orders(status: str | None = None):
    allowed_statuses = {"pending", "preparing", "ready"}

    if status is not None and status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Invalid kitchen order status filter.")

    query = """
        SELECT
            o.id,
            o.session_id,
            o.table_id,
            rt.table_number,
            o.order_number,
            o.order_type,
            o.created_by_type,
            o.status,
            o.total_amount,
            o.delivery_pin,
            o.delivery_pin_verified_at,
            o.created_at,
            o.updated_at,
            COUNT(oi.id) AS item_count
        FROM orders o
        JOIN restaurant_tables rt
            ON rt.id = o.table_id
        LEFT JOIN order_items oi
            ON oi.order_id = o.id
        WHERE o.status IN ('pending', 'preparing', 'ready')
    """

    params = []

    if status is not None:
        query += " AND o.status = %s"
        params.append(status)

    query += """
        GROUP BY
            o.id, o.session_id, o.table_id, rt.table_number,
            o.order_number, o.order_type, o.created_by_type,
            o.status, o.total_amount, o.delivery_pin,
            o.delivery_pin_verified_at, o.created_at, o.updated_at
        ORDER BY o.created_at ASC;
    """

    orders = fetch_all(query, tuple(params))

    return {
        "success": True,
        "count": len(orders),
        "data": orders,
    }


@app.get("/api/kitchen/orders/{order_id}")
def get_kitchen_order_detail(order_id: int):
    order = fetch_one(
        """
        SELECT
            o.id,
            o.session_id,
            o.table_id,
            rt.table_number,
            o.order_number,
            o.order_type,
            o.created_by_type,
            o.created_by_staff_id,
            o.status,
            o.total_amount,
            o.cancel_deadline,
            o.delivery_pin,
            o.delivery_pin_verified_at,
            o.created_at,
            o.updated_at
        FROM orders o
        JOIN restaurant_tables rt
            ON rt.id = o.table_id
        WHERE o.id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    items = fetch_all(
        """
        SELECT
            oi.id,
            oi.menu_item_id,
            mi.name AS menu_item_name,
            oi.quantity,
            oi.unit_price,
            oi.line_total,
            oi.item_status,
            oi.created_at
        FROM order_items oi
        JOIN menu_items mi
            ON mi.id = oi.menu_item_id
        WHERE oi.order_id = %s
        ORDER BY oi.id;
        """,
        (order_id,),
    )

    return {
        "success": True,
        "data": {
            **order,
            "items": items,
        },
    }

@app.get("/api/kitchen/ingredients")
def get_kitchen_ingredients():
    ingredients = fetch_all(
        """
        SELECT
            id,
            name,
            stock_quantity,
            unit
        FROM ingredients
        ORDER BY name ASC;
        """
    )

    return {
        "success": True,
        "count": len(ingredients),
        "data": ingredients,
    }

@app.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: int):
    order = fetch_one(
        """
        SELECT
            id,
            status,
            cancel_deadline
        FROM orders
        WHERE id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    if order["status"] == "cancelled":
     raise HTTPException(status_code=400, detail="Order is already cancelled.")

    if order["status"] == "paid":
        raise HTTPException(status_code=400, detail="Paid orders cannot be cancelled.")

    if order["status"] in ["preparing", "ready", "delivered"]:
        raise HTTPException(
            status_code=400,
            detail="Order already being processed, cannot cancel.",
        )

    cancel_deadline = order["cancel_deadline"]
    now = datetime.now(timezone.utc)

    if cancel_deadline.tzinfo is None:
        cancel_deadline = cancel_deadline.replace(tzinfo=timezone.utc)

    if now > cancel_deadline:
        raise HTTPException(
            status_code=400,
            detail="Cancel time window expired (5 minutes).",
        )

    def tx(conn, cur):
        cur.execute(
            """
            UPDATE orders
            SET status = 'cancelled',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (order_id,),
        )

        cur.execute(
            """
            UPDATE order_items
            SET item_status = 'cancelled'
            WHERE order_id = %s;
            """,
            (order_id,),
        )

        cur.execute(
            """
            INSERT INTO order_status_logs (
                order_id,
                old_status,
                new_status,
                note
            )
            VALUES (%s, %s, 'cancelled', %s);
            """,
            (order_id, order["status"], "Order cancelled by user"),
        )

        cur.execute(
            """
            SELECT session_id, table_id
            FROM orders
            WHERE id = %s;
            """,
            (order_id,),
        )
        cancelled_order = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(*) AS active_order_count
            FROM orders
            WHERE session_id = %s
            AND status NOT IN ('cancelled', 'paid');
            """,
            (cancelled_order["session_id"],),
        )
        active_count = cur.fetchone()["active_order_count"]

        if active_count == 0:
            cur.execute(
                """
                UPDATE table_sessions
                SET status = 'closed',
                    ended_at = CURRENT_TIMESTAMP
                WHERE id = %s;
                """,
                (cancelled_order["session_id"],),
            )

            cur.execute(
                """
                UPDATE restaurant_tables
                SET status = 'available'
                WHERE id = %s;
                """,
                (cancelled_order["table_id"],),
            )

    execute_transaction(tx)

    return {
        "success": True,
        "message": "Order cancelled successfully.",
    }

@app.get("/api/orders/{order_id}")
def get_order_detail(order_id: int):
    order = fetch_one(
        """
        SELECT
            id,
            session_id,
            table_id,
            order_number,
            order_type,
            created_by_type,
            created_by_staff_id,
            status,
            total_amount,
            cancel_deadline,
            delivery_pin,
            delivery_pin_verified_at,
            created_at,
            updated_at
        FROM orders
        WHERE id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    items = fetch_all(
        """
        SELECT
            oi.id,
            oi.menu_item_id,
            mi.name AS menu_item_name,
            oi.quantity,
            oi.unit_price,
            oi.line_total,
            oi.item_status,
            oi.created_at
        FROM order_items oi
        JOIN menu_items mi
            ON mi.id = oi.menu_item_id
        WHERE oi.order_id = %s
        ORDER BY oi.id;
        """,
        (order_id,),
    )

    return {
        "success": True,
        "data": {
            **order,
            "items": items,
        },
    }


@app.post("/api/orders/{order_id}/status")
def update_order_status(order_id: int, payload: OrderStatusUpdateRequest):
    new_status = payload.new_status
    valid_statuses = ["preparing", "ready"]

    if new_status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Invalid status.")

    order = fetch_one(
        """
        SELECT id, status
        FROM orders
        WHERE id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    current_status = order["status"]

    allowed_transitions = {
        "pending": "preparing",
        "preparing": "ready",
    }

    if current_status not in allowed_transitions:
        raise HTTPException(status_code=400, detail="Invalid current state.")

    if allowed_transitions[current_status] != new_status:
        raise HTTPException(status_code=400, detail="Invalid status transition.")

    def tx(conn, cur):
        if new_status == "preparing":
            cur.execute(
                """
                SELECT menu_item_id, quantity
                FROM order_items
                WHERE order_id = %s;
                """,
                (order_id,),
            )
            items = cur.fetchall()

            required_ingredients = []

            for item in items:
                cur.execute(
                    """
                    SELECT
                        mii.ingredient_id,
                        i.name AS ingredient_name,
                        i.stock_quantity,
                        mii.quantity_needed
                    FROM menu_item_ingredients mii
                    JOIN ingredients i
                        ON i.id = mii.ingredient_id
                    WHERE mii.menu_item_id = %s;
                    """,
                    (item["menu_item_id"],),
                )
                ingredients = cur.fetchall()

                for ing in ingredients:
                    total_needed = ing["quantity_needed"] * item["quantity"]

                    if ing["stock_quantity"] < total_needed:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Insufficient stock for ingredient '{ing['ingredient_name']}'. "
                                f"Required: {total_needed}, Available: {ing['stock_quantity']}."
                            ),
                        )

                    required_ingredients.append(
                        {
                            "ingredient_id": ing["ingredient_id"],
                            "total_needed": total_needed,
                        }
                    )

            for req in required_ingredients:
                cur.execute(
                    """
                    UPDATE ingredients
                    SET stock_quantity = stock_quantity - %s
                    WHERE id = %s;
                    """,
                    (req["total_needed"], req["ingredient_id"]),
                )

        cur.execute(
            """
            UPDATE orders
            SET status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (new_status, order_id),
        )

        cur.execute(
            """
            UPDATE order_items
            SET item_status = %s
            WHERE order_id = %s;
            """,
            (new_status, order_id),
        )

        cur.execute(
            """
            INSERT INTO order_status_logs (
                order_id,
                old_status,
                new_status,
                changed_by_staff_id,
                note
            )
            VALUES (%s, %s, %s, %s, %s);
            """,
            (
                order_id,
                current_status,
                new_status,
                payload.changed_by_staff_id,
                payload.note or "Status updated",
            ),
        )

        if new_status == "ready":
            cur.execute(
                """
                INSERT INTO notifications (
                    recipient_staff_id,
                    type,
                    title,
                    message,
                    related_order_id
                )
                VALUES (
                    1,
                    'order_ready',
                    'Order Ready',
                    'Order is ready for delivery',
                    %s
                );
                """,
                (order_id,),
            )

    execute_transaction(tx)

    return {
        "success": True,
        "message": f"Order updated to {new_status}"
    }





@app.post("/api/orders/{order_id}/deliver")
def deliver_order(order_id: int, payload: DeliverOrderRequest):
    waiter_id = payload.waiter_id
    delivery_pin = payload.delivery_pin

    order = fetch_one(
        """
        SELECT id, status, delivery_pin
        FROM orders
        WHERE id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    if order["status"] != "ready":
        raise HTTPException(
            status_code=400,
            detail="Order must be in 'ready' state to deliver.",
        )

    waiter = fetch_one(
        """
        SELECT id, pin_code, is_active
        FROM staff_users
        WHERE id = %s AND role = 'waiter';
        """,
        (waiter_id,),
    )

    if not waiter:
        raise HTTPException(status_code=404, detail="Waiter not found.")

    if not waiter["is_active"]:
        raise HTTPException(status_code=400, detail="Waiter is not active.")

    is_correct = order["delivery_pin"] == delivery_pin

    def tx(conn, cur):

        # PIN log
        cur.execute(
            """
            INSERT INTO delivery_verifications (
                order_id,
                waiter_id,
                entered_pin,
                is_successful
            )
            VALUES (%s, %s, %s, %s);
            """,
            (order_id, waiter_id, delivery_pin, is_correct),
        )

        if not is_correct:
            return

        # order status update
        cur.execute(
            """
            UPDATE orders
            SET status = 'delivered',
                delivery_pin_verified_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (order_id,),
        )

        # order items
        cur.execute(
            """
            UPDATE order_items
            SET item_status = 'delivered'
            WHERE order_id = %s;
            """,
            (order_id,),
        )

        # log
        cur.execute(
            """
            INSERT INTO order_status_logs (
                order_id,
                old_status,
                new_status,
                note
            )
            VALUES (%s, %s, 'delivered', %s);
            """,
            (order_id, "ready", "Order delivered"),
        )

    execute_transaction(tx)

    if not is_correct:
        raise HTTPException(status_code=400, detail="Invalid delivery PIN.")

    return {
        "success": True,
        "message": "Order delivered successfully."
    }

@app.post("/api/orders/{order_id}/request-payment")
def request_payment(order_id: int):
    order = fetch_one(
        """
        SELECT
            o.id,
            o.session_id,
            o.table_id,
            o.status,
            o.total_amount
        FROM orders o
        WHERE o.id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    if order["status"] != "delivered":
        raise HTTPException(
            status_code=400,
            detail="Payment can only be requested after delivery.",
        )

    existing_pending_payment = fetch_one(
        """
        SELECT id
        FROM payments
        WHERE order_id = %s AND status = 'pending';
        """,
        (order_id,),
    )

    if existing_pending_payment:
        raise HTTPException(
            status_code=400,
            detail="Payment request already exists for this order.",
        )

    def tx(conn, cur):
        cur.execute(
            """
            INSERT INTO waiter_calls (
                session_id,
                table_id,
                request_type,
                status
            )
            VALUES (%s, %s, 'payment', 'pending');
            """,
            (order["session_id"], order["table_id"]),
        )

        cur.execute(
            """
            INSERT INTO payments (
                order_id,
                session_id,
                table_id,
                payment_method,
                status,
                amount
            )
            VALUES (%s, %s, %s, 'card', 'pending', %s)
            RETURNING id;
            """,
            (
                order["id"],
                order["session_id"],
                order["table_id"],
                order["total_amount"],
            ),
        )
        payment = cur.fetchone()

        cur.execute(
            """
            INSERT INTO notifications (
                recipient_staff_id,
                type,
                title,
                message,
                related_order_id,
                related_table_id
            )
            VALUES (
                1,
                'payment_request',
                'Payment Request',
                'Table requested payment.',
                %s,
                %s
            );
            """,
            (order["id"], order["table_id"]),
        )

        return {
            "payment_id": payment["id"],
            "order_id": order["id"],
            "session_id": order["session_id"],
            "table_id": order["table_id"],
            "amount": float(order["total_amount"]),
            "status": "pending",
        }

    result = execute_transaction(tx)

    return {
        "success": True,
        "message": "Payment request created successfully.",
        "data": result,
    }


@app.post("/api/waiter/move-table")
def move_table(payload: MoveTableRequest):
    if payload.from_table_id == payload.to_table_id:
        raise HTTPException(status_code=400, detail="Old table and new table cannot be same.")

    waiter = fetch_one(
        """
        SELECT id, role, is_active
        FROM staff_users
        WHERE id = %s AND role = 'waiter';
        """,
        (payload.waiter_id,),
    )

    if not waiter:
        raise HTTPException(status_code=404, detail="Waiter not found.")

    if not waiter["is_active"]:
        raise HTTPException(status_code=400, detail="Waiter is not active.")

    from_table = fetch_one(
        """
        SELECT id, table_number, status
        FROM restaurant_tables
        WHERE id = %s;
        """,
        (payload.from_table_id,),
    )

    if not from_table:
        raise HTTPException(status_code=404, detail="Old table not found.")

    to_table = fetch_one(
        """
        SELECT id, table_number, status
        FROM restaurant_tables
        WHERE id = %s;
        """,
        (payload.to_table_id,),
    )

    if not to_table:
        raise HTTPException(status_code=404, detail="New table not found.")

    active_session = fetch_one(
        """
        SELECT id
        FROM table_sessions
        WHERE table_id = %s AND status = 'active'
        ORDER BY started_at DESC
        LIMIT 1;
        """,
        (payload.from_table_id,),
    )

    if not active_session:
        raise HTTPException(status_code=404, detail="Old table has no active session.")

    target_session = fetch_one(
        """
        SELECT id
        FROM table_sessions
        WHERE table_id = %s AND status = 'active'
        ORDER BY started_at DESC
        LIMIT 1;
        """,
        (payload.to_table_id,),
    )

    if target_session:
        raise HTTPException(status_code=400, detail="New table already has an active session.")

    session_id = active_session["id"]

    def tx(conn, cur):
        cur.execute(
            """
            UPDATE table_sessions
            SET table_id = %s
            WHERE id = %s;
            """,
            (payload.to_table_id, session_id),
        )

        cur.execute(
            """
            UPDATE orders
            SET table_id = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE session_id = %s
              AND status NOT IN ('paid', 'cancelled');
            """,
            (payload.to_table_id, session_id),
        )

        cur.execute(
            """
            UPDATE waiter_calls
            SET table_id = %s
            WHERE session_id = %s
              AND status IN ('pending', 'seen');
            """,
            (payload.to_table_id, session_id),
        )

        cur.execute(
            """
            UPDATE payments
            SET table_id = %s
            WHERE session_id = %s
              AND status = 'pending';
            """,
            (payload.to_table_id, session_id),
        )

        cur.execute(
            """
            UPDATE restaurant_tables
            SET status = 'available'
            WHERE id = %s;
            """,
            (payload.from_table_id,),
        )

        cur.execute(
            """
            UPDATE restaurant_tables
            SET status = 'occupied'
            WHERE id = %s;
            """,
            (payload.to_table_id,),
        )

    execute_transaction(tx)

    return {
        "success": True,
        "message": "Table changed successfully.",
        "data": {
            "session_id": session_id,
            "from_table_id": payload.from_table_id,
            "to_table_id": payload.to_table_id,
        },
    }


@app.get("/api/waiter/dashboard")
def get_waiter_dashboard():
    pending_calls = fetch_all(
        """
        SELECT
            wc.id,
            wc.session_id,
            wc.table_id,
            rt.table_number,
            wc.request_type,
            wc.status,
            wc.created_at,
            wc.handled_by_staff_id,
            wc.handled_at
        FROM waiter_calls wc
        JOIN restaurant_tables rt
            ON rt.id = wc.table_id
        WHERE wc.status IN ('pending', 'seen')
        ORDER BY wc.created_at ASC;
        """
    )

    ready_orders = fetch_all(
        """
        SELECT
            o.id,
            o.session_id,
            o.table_id,
            rt.table_number,
            o.order_number,
            o.order_type,
            o.status,
            o.total_amount,
            o.delivery_pin,
            o.delivery_pin_verified_at,
            o.created_at,
            o.updated_at
        FROM orders o
        JOIN restaurant_tables rt
            ON rt.id = o.table_id
        WHERE o.status = 'ready'
        ORDER BY o.created_at ASC;
        """
    )

    pending_payments = fetch_all(
        """
        SELECT
            p.id,
            p.order_id,
            p.session_id,
            p.table_id,
            rt.table_number,
            p.payment_method,
            p.status,
            p.amount,
            p.confirmed_by_waiter_id,
            p.confirmation_pin,
            p.paid_at,
            p.created_at
        FROM payments p
        JOIN restaurant_tables rt
            ON rt.id = p.table_id
        WHERE p.status = 'pending'
        ORDER BY p.created_at ASC;
        """
    )

    unread_notifications = fetch_all(
        """
        SELECT
            n.id,
            n.recipient_staff_id,
            n.type,
            n.title,
            n.message,
            n.related_order_id,
            n.related_table_id,
            n.is_read,
            n.created_at
        FROM notifications n
        JOIN staff_users s
            ON s.id = n.recipient_staff_id
        WHERE n.is_read = FALSE
          AND s.role = 'waiter'
          AND s.is_active = TRUE
        ORDER BY n.created_at DESC;
        """
    )

    return {
        "success": True,
        "data": {
            "pending_calls": pending_calls,
            "ready_orders": ready_orders,
            "pending_payments": pending_payments,
            "unread_notifications": unread_notifications,
        },
    }


@app.get("/api/waiter/calls")
def get_waiter_calls(status: str | None = None, request_type: str | None = None):
    query = """
        SELECT
            wc.id,
            wc.session_id,
            wc.table_id,
            rt.table_number,
            wc.request_type,
            wc.status,
            wc.created_at,
            wc.handled_by_staff_id,
            wc.handled_at
        FROM waiter_calls wc
        JOIN restaurant_tables rt
            ON rt.id = wc.table_id
        WHERE 1=1
    """
    params = []

    if status:
        if status not in VALID_WAITER_CALL_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid waiter call status.")
        query += " AND wc.status = %s"
        params.append(status)

    if request_type:
        if request_type not in VALID_WAITER_CALL_TYPES:
            raise HTTPException(status_code=400, detail="Invalid waiter call type.")
        query += " AND wc.request_type = %s"
        params.append(request_type)

    query += " ORDER BY wc.created_at ASC;"

    calls = fetch_all(query, tuple(params))

    return {
        "success": True,
        "count": len(calls),
        "data": calls,
    }


@app.post("/api/waiter/calls/{call_id}/seen")
def mark_waiter_call_seen(call_id: int, payload: WaiterCallActionRequest):
    call = fetch_one(
        """
        SELECT id, status
        FROM waiter_calls
        WHERE id = %s;
        """,
        (call_id,),
    )

    if not call:
        raise HTTPException(status_code=404, detail="Waiter call not found.")

    if call["status"] == "completed":
        raise HTTPException(status_code=400, detail="Completed waiter call cannot be changed.")

    execute_query(
        """
        UPDATE waiter_calls
        SET status = 'seen',
            handled_by_staff_id = %s
        WHERE id = %s;
        """,
        (payload.staff_id, call_id),
    )

    return {
        "success": True,
        "message": "Waiter call marked as seen.",
    }


@app.post("/api/waiter/calls/{call_id}/complete")
def complete_waiter_call(call_id: int, payload: WaiterCallActionRequest):
    call = fetch_one(
        """
        SELECT id, status
        FROM waiter_calls
        WHERE id = %s;
        """,
        (call_id,),
    )

    if not call:
        raise HTTPException(status_code=404, detail="Waiter call not found.")

    if call["status"] == "completed":
        raise HTTPException(status_code=400, detail="Waiter call already completed.")

    execute_query(
        """
        UPDATE waiter_calls
        SET status = 'completed',
            handled_by_staff_id = %s,
            handled_at = CURRENT_TIMESTAMP
        WHERE id = %s;
        """,
        (payload.staff_id, call_id),
    )

    return {
        "success": True,
        "message": "Waiter call completed successfully.",
    }


@app.get("/api/waiter/orders-ready")
def get_waiter_ready_orders():
    orders = fetch_all(
        """
        SELECT
            o.id,
            o.session_id,
            o.table_id,
            rt.table_number,
            o.order_number,
            o.order_type,
            o.status,
            o.total_amount,
            o.delivery_pin,
            o.delivery_pin_verified_at,
            o.created_at,
            o.updated_at
        FROM orders o
        JOIN restaurant_tables rt
            ON rt.id = o.table_id
        WHERE o.status = 'ready'
        ORDER BY o.created_at ASC;
        """
    )

    return {
        "success": True,
        "count": len(orders),
        "data": orders,
    }


@app.get("/api/waiter/payments-pending")
def get_waiter_pending_payments():
    payments = fetch_all(
        """
        SELECT
            p.id,
            p.order_id,
            p.session_id,
            p.table_id,
            rt.table_number,
            p.payment_method,
            p.status,
            p.amount,
            p.confirmed_by_waiter_id,
            p.confirmation_pin,
            p.paid_at,
            p.created_at
        FROM payments p
        JOIN restaurant_tables rt
            ON rt.id = p.table_id
        WHERE p.status = 'pending'
        ORDER BY p.created_at ASC;
        """
    )

    return {
        "success": True,
        "count": len(payments),
        "data": payments,
    }

@app.get("/api/staff/{staff_id}/notifications")
def get_staff_notifications(staff_id: int):
    notifications = fetch_all(
        """
        SELECT
            id,
            recipient_staff_id,
            type,
            title,
            message,
            related_order_id,
            related_table_id,
            is_read,
            created_at
        FROM notifications
        WHERE recipient_staff_id = %s
        ORDER BY created_at DESC;
        """,
        (staff_id,),
    )

    return {
        "success": True,
        "count": len(notifications),
        "data": notifications,
    }


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_as_read(notification_id: int):
    notification = fetch_one(
        """
        SELECT id
        FROM notifications
        WHERE id = %s;
        """,
        (notification_id,),
    )

    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found.")

    execute_query(
        """
        UPDATE notifications
        SET is_read = TRUE
        WHERE id = %s;
        """,
        (notification_id,),
    )

    return {
        "success": True,
        "message": "Notification marked as read.",
    }

@app.post("/api/orders/{order_id}/pay")
def pay_order(order_id: int, payload: PayOrderRequest):
    waiter_id = payload.waiter_id
    pin = payload.pin
    payment_method = payload.payment_method
    if payment_method not in ["card", "cash"]:
        raise HTTPException(status_code=400, detail="Invalid payment method.")

    order = fetch_one(
        """
        SELECT
            id,
            session_id,
            table_id,
            status,
            total_amount
        FROM orders
        WHERE id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    if order["status"] != "delivered":
        raise HTTPException(
            status_code=400,
            detail="Only delivered orders can be paid.",
        )

    waiter = fetch_one(
        """
        SELECT id, pin_code, is_active
        FROM staff_users
        WHERE id = %s AND role = 'waiter';
        """,
        (waiter_id,),
    )

    if not waiter:
        raise HTTPException(status_code=404, detail="Waiter not found.")

    if not waiter["is_active"]:
        raise HTTPException(status_code=400, detail="Waiter is not active.")

    payment = fetch_one(
        """
        SELECT id, status
        FROM payments
        WHERE order_id = %s
        ORDER BY created_at DESC
        LIMIT 1;
        """,
        (order_id,),
    )

    if not payment:
        raise HTTPException(status_code=404, detail="No payment request found for this order.")

    if payment["status"] == "paid":
        raise HTTPException(status_code=400, detail="Order is already paid.")

    is_correct = waiter["pin_code"] == pin

    def tx(conn, cur):
        if not is_correct:
            cur.execute(
                """
                UPDATE payments
                SET confirmation_pin = %s,
                    status = 'rejected'
                WHERE id = %s;
                """,
                (pin, payment["id"]),
            )

            cur.execute(
                """
                INSERT INTO notifications (
                    recipient_staff_id,
                    type,
                    title,
                    message,
                    related_order_id,
                    related_table_id
                )
                VALUES (
                    1,
                    'payment_rejected',
                    'Payment Rejected',
                    'Payment confirmation failed. Customer can request payment again.',
                    %s,
                    %s
                );
                """,
                (order_id, order["table_id"]),
            )

            return

        cur.execute(
            """
            UPDATE payments
            SET payment_method = %s,
                status = 'paid',
                confirmed_by_waiter_id = %s,
                confirmation_pin = %s,
                paid_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (payment_method, waiter_id, pin, payment["id"]),
        )

        cur.execute(
            """
            UPDATE orders
            SET status = 'paid',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (order_id,),
        )

        cur.execute(
            """
            INSERT INTO order_status_logs (
                order_id,
                old_status,
                new_status,
                note
            )
            VALUES (%s, %s, 'paid', %s);
            """,
            (order_id, "delivered", "Payment received"),
        )

        cur.execute(
            """
            UPDATE waiter_calls
            SET status = 'completed',
                handled_by_staff_id = %s,
                handled_at = CURRENT_TIMESTAMP
            WHERE table_id = %s
              AND request_type = 'payment'
              AND status IN ('pending', 'seen');
            """,
            (waiter_id, order["table_id"]),
        )

        cur.execute(
            """
            UPDATE table_sessions
            SET status = 'closed',
                ended_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (order["session_id"],),
        )

        cur.execute(
            """
            UPDATE restaurant_tables
            SET status = 'available'
            WHERE id = %s;
            """,
            (order["table_id"],),
        )

    execute_transaction(tx)

    if not is_correct:
        raise HTTPException(status_code=400, detail="Invalid PIN.")

    return {
        "success": True,
        "message": "Payment completed successfully.",
    }


@app.get("/api/tables/{table_id}/dashboard")
def get_table_dashboard(table_id: int):
    table = fetch_one(
        """
        SELECT
            id,
            table_number,
            name,
            status,
            created_at
        FROM restaurant_tables
        WHERE id = %s;
        """,
        (table_id,),
    )

    if not table:
        raise HTTPException(status_code=404, detail="Table not found.")

    session = fetch_one(
        """
        SELECT
            id,
            table_id,
            started_at,
            ended_at,
            status
        FROM table_sessions
        WHERE table_id = %s
          AND status = 'active'
        ORDER BY started_at DESC
        LIMIT 1;
        """,
        (table_id,),
    )

    active_orders = []
    active_calls = []
    latest_payment = None

    if session:
        active_orders = fetch_all(
            """
            SELECT
                o.id,
                o.order_number,
                o.order_type,
                o.created_by_type,
                o.created_by_staff_id,
                o.status,
                o.total_amount,
                o.cancel_deadline,
                o.delivery_pin,
                o.delivery_pin_verified_at,
                o.created_at,
                o.updated_at
            FROM orders o
            WHERE o.session_id = %s
              AND o.status IN ('pending', 'preparing', 'ready', 'delivered')
            ORDER BY o.created_at DESC;
            """,
            (session["id"],),
        )

        for order in active_orders:
            items = fetch_all(
                """
                SELECT
                    oi.id,
                    oi.menu_item_id,
                    mi.name AS menu_item_name,
                    oi.quantity,
                    oi.unit_price,
                    oi.line_total,
                    oi.item_status,
                    oi.created_at
                FROM order_items oi
                JOIN menu_items mi
                    ON mi.id = oi.menu_item_id
                WHERE oi.order_id = %s
                ORDER BY oi.id;
                """,
                (order["id"],),
            )
            order["items"] = items



        active_calls = fetch_all(
            """
            SELECT
                wc.id,
                wc.session_id,
                wc.table_id,
                wc.request_type,
                wc.status,
                wc.created_at,
                wc.handled_by_staff_id,
                wc.handled_at
            FROM waiter_calls wc
            WHERE wc.session_id = %s
              AND wc.status IN ('pending', 'seen')
            ORDER BY wc.created_at DESC;
            """,
            (session["id"],),
        )

        latest_payment = fetch_one(
            """
            SELECT
                p.id,
                p.order_id,
                p.session_id,
                p.table_id,
                p.payment_method,
                p.status,
                p.amount,
                p.confirmed_by_waiter_id,
                p.confirmation_pin,
                p.paid_at,
                p.created_at
            FROM payments p
            WHERE p.session_id = %s
            ORDER BY p.created_at DESC
            LIMIT 1;
            """,
            (session["id"],),
        )

    return {
        "success": True,
        "data": {
            "table": table,
            "active_session": session,
            "active_orders": active_orders,
            "active_waiter_calls": active_calls,
            "latest_payment": latest_payment,
        },
    }


@app.post("/api/tables/{table_id}/call-waiter")
def call_waiter(table_id: int, payload: WaiterCallCreateRequest):
    if payload.request_type not in VALID_WAITER_CALL_TYPES:
        raise HTTPException(status_code=400, detail="Invalid waiter call type.")

    table = fetch_one(
        """
        SELECT id, table_number, status
        FROM restaurant_tables
        WHERE id = %s;
        """,
        (table_id,),
    )

    if not table:
        raise HTTPException(status_code=404, detail="Table not found.")

    session = fetch_one(
        """
        SELECT id, table_id, status
        FROM table_sessions
        WHERE table_id = %s
          AND status = 'active'
        ORDER BY started_at DESC
        LIMIT 1;
        """,
        (table_id,),
    )

    current_session_id = session["id"] if session else None

    existing_pending = None
    if current_session_id is not None:
        existing_pending = fetch_one(
            """
            SELECT id
            FROM waiter_calls
            WHERE session_id = %s
              AND request_type = %s
              AND status IN ('pending', 'seen')
            LIMIT 1;
            """,
            (current_session_id, payload.request_type),
        )

    if existing_pending:
        raise HTTPException(
            status_code=400,
            detail=f"An active '{payload.request_type}' waiter call already exists.",
        )

    def tx(conn, cur):
        local_session_id = current_session_id

        if local_session_id is None:
            cur.execute(
                """
                INSERT INTO table_sessions (table_id, status)
                VALUES (%s, 'active')
                RETURNING id;
                """,
                (table_id,),
            )
            local_session_id = cur.fetchone()["id"]

            cur.execute(
                """
                UPDATE restaurant_tables
                SET status = 'occupied'
                WHERE id = %s;
                """,
                (table_id,),
            )

        cur.execute(
            """
            INSERT INTO waiter_calls (
                session_id,
                table_id,
                request_type,
                status
            )
            VALUES (%s, %s, %s, 'pending')
            RETURNING id, session_id, table_id, request_type, status, created_at;
            """,
            (local_session_id, table_id, payload.request_type),
        )
        created_call = cur.fetchone()

        cur.execute(
            """
            SELECT id
            FROM staff_users
            WHERE role = 'waiter' AND is_active = TRUE;
            """
        )
        active_waiters = cur.fetchall()

        for waiter in active_waiters:
            cur.execute(
                """
                INSERT INTO notifications (
                    recipient_staff_id,
                    type,
                    title,
                    message,
                    related_table_id
                )
                VALUES (%s, %s, %s, %s, %s);
                """,
                (
                    waiter["id"],
                    "waiter_call",
                    "Waiter Call",
                    f"Table {table['table_number']} requested {payload.request_type}.",
                    table_id,
                ),
            )

        return created_call

    result = execute_transaction(tx)

    return {
        "success": True,
        "message": "Waiter call created successfully.",
        "data": result,
    }



@app.get("/api/tables/{table_id}")
def get_table_detail(table_id: int):
    table = fetch_one(
        """
        SELECT
            id,
            table_number,
            name,
            status,
            created_at
        FROM restaurant_tables
        WHERE id = %s;
        """,
        (table_id,),
    )

    if not table:
        raise HTTPException(status_code=404, detail="Table not found.")

    return {
        "success": True,
        "data": table,
    }


@app.post("/api/kitchen/ingredients")
def create_kitchen_ingredient(payload: IngredientCreateRequest):
    name = payload.name.strip()
    unit = payload.unit.strip()

    if not name:
        raise HTTPException(status_code=400, detail="Ingredient name is required.")

    if not unit:
        raise HTTPException(status_code=400, detail="Ingredient unit is required.")

    existing = fetch_one(
        """
        SELECT id, name, stock_quantity, unit
        FROM ingredients
        WHERE LOWER(name) = LOWER(%s);
        """,
        (name,),
    )

    if existing:
        updated = fetch_one(
            """
            UPDATE ingredients
            SET stock_quantity = stock_quantity + %s,
                unit = %s
            WHERE id = %s
            RETURNING id, name, stock_quantity, unit;
            """,
            (payload.stock_quantity, unit, existing["id"]),
        )

        return {
            "success": True,
            "message": "Ingredient stock updated successfully.",
            "data": updated,
        }

    created = fetch_one(
        """
        INSERT INTO ingredients (
            name,
            stock_quantity,
            unit
        )
        VALUES (%s, %s, %s)
        RETURNING id, name, stock_quantity, unit;
        """,
        (name, payload.stock_quantity, unit),
    )

    return {
        "success": True,
        "message": "Ingredient created successfully.",
        "data": created,
    }

@app.post("/api/orders/{order_id}/items/{menu_item_id}/review")
def create_order_item_review(
    order_id: int,
    menu_item_id: int,
    payload: MenuItemReviewCreateRequest,
):
    order = fetch_one(
        """
        SELECT id, status
        FROM orders
        WHERE id = %s;
        """,
        (order_id,),
    )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    if order["status"] != "delivered":
        raise HTTPException(
            status_code=400,
            detail="You can review items only after the order is delivered and before payment.",
        )

    ordered_item = fetch_one(
        """
        SELECT oi.id
        FROM order_items oi
        WHERE oi.order_id = %s
          AND oi.menu_item_id = %s
        LIMIT 1;
        """,
        (order_id, menu_item_id),
    )

    if not ordered_item:
        raise HTTPException(
            status_code=400,
            detail="You can only review items from your own delivered order.",
        )

    review = fetch_one(
        """
        INSERT INTO menu_item_reviews (
            menu_item_id,
            rating,
            comment
        )
        VALUES (%s, %s, %s)
        RETURNING id, menu_item_id, rating, comment, created_at;
        """,
        (menu_item_id, payload.rating, payload.comment),
    )

    return {
        "success": True,
        "message": "Review created successfully.",
        "data": review,
    }