from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import uuid
from datetime import datetime

app = FastAPI(title="E-Commerce Builder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-memory DB ────────────────────────────────────────────────
stores = {}
products = {}
orders = {}
reviews = {}
carts = {}

# ─── Models ──────────────────────────────────────────────────────
class Store(BaseModel):
    name: str
    description: Optional[str] = ""
    owner: Optional[str] = ""

class StoreUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    theme: Optional[dict] = None
    hero_text: Optional[str] = None
    hero_subtitle: Optional[str] = None
    banner_color: Optional[str] = None
    page_config: Optional[str] = None  # ← add this

class Product(BaseModel):
    store_id: str
    name: str
    description: Optional[str] = ""
    price: float
    stock: int = 0
    category: Optional[str] = ""
    image_url: Optional[str] = ""

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    category: Optional[str] = None
    image_url: Optional[str] = None

class OrderItem(BaseModel):
    product_id: str
    quantity: int

class Order(BaseModel):
    store_id: str
    customer_name: str
    customer_email: str
    items: List[OrderItem]
    address: Optional[str] = ""

class Review(BaseModel):
    store_id: str
    product_id: Optional[str] = None
    customer_name: str
    rating: int
    comment: Optional[str] = ""

class CartItem(BaseModel):
    product_id: str
    quantity: int

# ─── Store Endpoints ─────────────────────────────────────────────
@app.post("/stores")
def create_store(store: Store):
    store_id = str(uuid.uuid4())[:8]
    stores[store_id] = {
        "id": store_id,
        "name": store.name,
        "description": store.description,
        "owner": store.owner,
        "theme": {},
        "hero_text": f"Welcome to {store.name}",
        "hero_subtitle": store.description,
        "banner_color": "#6366f1",
        "created_at": datetime.now().isoformat(),
    }
    return stores[store_id]

@app.get("/stores")
def list_stores():
    return list(stores.values())

@app.get("/stores/{store_id}")
def get_store(store_id: str):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    return stores[store_id]

@app.put("/stores/{store_id}")
def update_store(store_id: str, update: StoreUpdate):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    for k, v in update.dict(exclude_none=True).items():
        stores[store_id][k] = v
    return stores[store_id]

@app.delete("/stores/{store_id}")
def delete_store(store_id: str):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    del stores[store_id]
    return {"message": "Store deleted"}

# ─── Product Endpoints ────────────────────────────────────────────
@app.post("/products")
def create_product(product: Product):
    if product.store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    product_id = str(uuid.uuid4())[:8]
    products[product_id] = {
        "id": product_id,
        **product.dict(),
        "created_at": datetime.now().isoformat(),
    }
    return products[product_id]

@app.get("/products")
def list_products(store_id: Optional[str] = None):
    all_products = list(products.values())
    if store_id:
        all_products = [p for p in all_products if p["store_id"] == store_id]
    return all_products

@app.get("/products/{product_id}")
def get_product(product_id: str):
    if product_id not in products:
        raise HTTPException(status_code=404, detail="Product not found")
    return products[product_id]

@app.put("/products/{product_id}")
def update_product(product_id: str, update: ProductUpdate):
    if product_id not in products:
        raise HTTPException(status_code=404, detail="Product not found")
    for k, v in update.dict(exclude_none=True).items():
        products[product_id][k] = v
    return products[product_id]

@app.delete("/products/{product_id}")
def delete_product(product_id: str):
    if product_id not in products:
        raise HTTPException(status_code=404, detail="Product not found")
    del products[product_id]
    return {"message": "Product deleted"}

# ─── Order / Checkout Endpoints ───────────────────────────────────
@app.post("/orders")
def create_order(order: Order):
    if order.store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    order_id = str(uuid.uuid4())[:8]
    total = 0
    line_items = []
    for item in order.items:
        if item.product_id not in products:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        product = products[item.product_id]
        if product["stock"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for {product['name']}")
        products[item.product_id]["stock"] -= item.quantity
        subtotal = product["price"] * item.quantity
        total += subtotal
        line_items.append({
            "product_id": item.product_id,
            "product_name": product["name"],
            "quantity": item.quantity,
            "unit_price": product["price"],
            "subtotal": subtotal,
        })
    orders[order_id] = {
        "id": order_id,
        "store_id": order.store_id,
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "address": order.address,
        "items": line_items,
        "total": total,
        "status": "confirmed",
        "created_at": datetime.now().isoformat(),
    }
    return orders[order_id]

@app.get("/orders")
def list_orders(store_id: Optional[str] = None):
    all_orders = list(orders.values())
    if store_id:
        all_orders = [o for o in all_orders if o["store_id"] == store_id]
    return all_orders

# ─── Review Endpoints ─────────────────────────────────────────────
@app.post("/reviews")
def create_review(review: Review):
    if review.store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    review_id = str(uuid.uuid4())[:8]
    reviews[review_id] = {
        "id": review_id,
        **review.dict(),
        "created_at": datetime.now().isoformat(),
    }
    return reviews[review_id]

@app.get("/reviews")
def list_reviews(store_id: Optional[str] = None):
    all_reviews = list(reviews.values())
    if store_id:
        all_reviews = [r for r in all_reviews if r["store_id"] == store_id]
    return all_reviews

@app.get("/reviews/stats/{store_id}")
def review_stats(store_id: str):
    store_reviews = [r for r in reviews.values() if r["store_id"] == store_id]
    if not store_reviews:
        return {"average_rating": 0, "total_reviews": 0, "rating_breakdown": {}}
    avg = sum(r["rating"] for r in store_reviews) / len(store_reviews)
    breakdown = {str(i): sum(1 for r in store_reviews if r["rating"] == i) for i in range(1, 6)}
    return {
        "average_rating": round(avg, 2),
        "total_reviews": len(store_reviews),
        "rating_breakdown": breakdown,
    }

# ─── Analytics ────────────────────────────────────────────────────
@app.get("/analytics/{store_id}")
def get_analytics(store_id: str):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Store not found")
    store_orders = [o for o in orders.values() if o["store_id"] == store_id]
    store_products = [p for p in products.values() if p["store_id"] == store_id]
    store_reviews = [r for r in reviews.values() if r["store_id"] == store_id]
    total_revenue = sum(o["total"] for o in store_orders)
    avg_rating = (sum(r["rating"] for r in store_reviews) / len(store_reviews)) if store_reviews else 0
    return {
        "total_orders": len(store_orders),
        "total_revenue": round(total_revenue, 2),
        "total_products": len(store_products),
        "total_reviews": len(store_reviews),
        "average_rating": round(avg_rating, 2),
        "recent_orders": store_orders[-5:][::-1],
    }
