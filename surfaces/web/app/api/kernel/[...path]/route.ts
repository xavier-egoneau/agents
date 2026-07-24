const kernelUrl = process.env.AMK_KERNEL_URL || "http://127.0.0.1:8765";

async function proxy(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const search = new URL(request.url).search;
  const target = `${kernelUrl}/api/${path.join("/")}${search}`;
  const body = request.method === "GET" ? undefined : await request.text();
  try {
    const response = await fetch(target, {
      method: request.method,
      headers: body ? { "content-type": "application/json" } : undefined,
      body,
    });
    return new Response(response.body, {
      status: response.status,
      headers: { "content-type": response.headers.get("content-type") || "application/json" },
    });
  } catch {
    return Response.json(
      { detail: "Kernel indisponible. Lancez `amk serve` sur le port 8765." },
      { status: 503 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
