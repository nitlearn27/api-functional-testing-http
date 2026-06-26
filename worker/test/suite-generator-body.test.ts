import { describe, expect, it } from "vitest";
import { generateTestSuite } from "../src/suite/generate.js";
import { readTestSuite } from "../src/suite/parse.js";

// A POST whose body requires a string, an array of objects, and a nested object — none of which
// carry an `example`. The generated positive body must still include EVERY required field with a
// valid value (recursively), so the create request is actually valid.
const SPEC = `
openapi: 3.0.0
info: { title: Orders, version: "1.0" }
servers:
  - url: https://api.example.com/api
paths:
  /orders:
    post:
      summary: Create an order
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [customerId, items, shippingAddress]
              properties:
                customerId: { type: string }
                items:
                  type: array
                  items: { $ref: "#/components/schemas/OrderItem" }
                shippingAddress: { $ref: "#/components/schemas/ShippingAddress" }
                notes: { type: string }
                deliveryDate: { type: string, format: date }
      responses:
        "201": { description: Created }
components:
  schemas:
    OrderItem:
      type: object
      required: [productId, quantity]
      properties:
        productId: { type: string }
        quantity: { type: integer, minimum: 1 }
    ShippingAddress:
      type: object
      required: [street, city, country, postalCode]
      properties:
        street: { type: string }
        city: { type: string }
        country: { type: string }
        postalCode: { type: string }
`;

function cases() {
  const { bytes } = generateTestSuite(SPEC);
  return readTestSuite(new Uint8Array(bytes)).cases;
}

describe("generate_test_suite — request body", () => {
  it("positive POST body includes every required field (recursively), optionals omitted", () => {
    const positive = cases().find((c) => c.method === "POST" && c.expected_status === 201);
    expect(positive!.body).toEqual({
      customerId: "sample",
      items: [{ productId: "sample", quantity: 1 }],
      shippingAddress: { street: "sample", city: "sample", country: "sample", postalCode: "sample" },
    });
  });

  it("a 'missing required X' negative drops only X, keeping the other required fields", () => {
    const missingCustomer = cases().find((c) => /missing required 'customerId'/.test(c.description ?? ""));
    expect(missingCustomer).toBeDefined();
    const body = missingCustomer!.body as Record<string, unknown>;
    expect("customerId" in body).toBe(false);
    expect(body.items).toEqual([{ productId: "sample", quantity: 1 }]);
    expect(body.shippingAddress).toBeDefined();
  });
});
