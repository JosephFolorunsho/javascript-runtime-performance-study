export class ValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = "ValidationError";
  }
}

function requireFiniteNumber(value, fieldName) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ValidationError(`${fieldName} must be a finite number`);
  }
}

export function processOrder(payload) {
  if (
    payload === null ||
    typeof payload !== "object" ||
    Array.isArray(payload)
  ) {
    throw new ValidationError("Request body must be a JSON object");
  }

  requireFiniteNumber(payload.customerId, "customerId");

  requireFiniteNumber(payload.discountRate, "discountRate");

  if (payload.discountRate < 0 || payload.discountRate > 1) {
    throw new ValidationError("discountRate must be between 0 and 1");
  }

  if (!Array.isArray(payload.items) || payload.items.length === 0) {
    throw new ValidationError("items must be a non-empty array");
  }

  let itemCount = 0;
  let subtotalCents = 0;

  for (const item of payload.items) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) {
      throw new ValidationError("Each item must be a JSON object");
    }

    requireFiniteNumber(item.productId, "productId");

    requireFiniteNumber(item.quantity, "quantity");

    requireFiniteNumber(item.price, "price");

    if (!Number.isInteger(item.quantity) || item.quantity <= 0) {
      throw new ValidationError("quantity must be a positive integer");
    }

    if (item.price < 0) {
      throw new ValidationError("price cannot be negative");
    }

    const unitPriceCents = Math.round(item.price * 100);

    itemCount += item.quantity;

    subtotalCents += unitPriceCents * item.quantity;
  }

  const discountCents = Math.round(subtotalCents * payload.discountRate);

  const totalCents = subtotalCents - discountCents;

  return {
    customerId: payload.customerId,
    itemCount,
    subtotal: subtotalCents / 100,
    discount: discountCents / 100,
    total: totalCents / 100,
    status: "processed",
  };
}
