import { describe, expect, it } from "vitest";
import * as XLSX from "xlsx";

// De-risk: SheetJS must read and write .xlsx purely from in-memory buffers (no fs), which is
// what the Workers runtime requires. We never pass a filename to XLSX.read/write.
describe("SheetJS in-memory round-trip", () => {
  it("writes a workbook to a buffer and reads it back", () => {
    const wb = XLSX.utils.book_new();
    const ws = XLSX.utils.aoa_to_sheet([
      ["Basepath", "https://api.example.com"],
      ["test_id", "method", "url"],
      ["TC-001", "GET", "/products"],
    ]);
    XLSX.utils.book_append_sheet(wb, ws, "tests");

    const out = XLSX.write(wb, { type: "array", bookType: "xlsx" }) as Uint8Array;
    expect(out.byteLength).toBeGreaterThan(0);

    const back = XLSX.read(out, { type: "array" });
    const rows = XLSX.utils.sheet_to_json<unknown[]>(back.Sheets["tests"], { header: 1 });
    expect(rows[0]).toEqual(["Basepath", "https://api.example.com"]);
    expect(rows[2]).toEqual(["TC-001", "GET", "/products"]);
  });
});
