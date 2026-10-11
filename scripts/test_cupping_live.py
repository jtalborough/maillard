#!/usr/bin/env python3
"""Live test script for cupping MCP tools.

Run with: python scripts/test_cupping_live.py
"""

import asyncio
from unittest.mock import MagicMock

from maillard.db.repository import RoastRepository
from maillard.db.schemas import RoastCreate
from maillard.mcp import lifespan
from maillard.mcp.tools.cupping import (
    add_cupping,
    delete_cupping,
    get_cupping,
    list_cuppings,
    update_cupping,
)


def _make_mock_context(state):
    """Create a mock MCP context for testing."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": state}
    return ctx


async def main():
    print("=" * 60)
    print("Cupping Tools Live Test")
    print("=" * 60)

    async with lifespan() as state:
        ctx = _make_mock_context(state)

        # Create a test roast first
        print("\n1. Creating test roast...")
        async with state.session() as session:
            repo = RoastRepository(session)
            roast = await repo.create(RoastCreate())
            await session.commit()
            roast_id = roast.id
        print(f"   Created roast: {roast_id}")

        # Test add_cupping
        print("\n2. Adding cupping with scores...")
        cupping = await add_cupping(
            ctx,
            roast_id=roast_id,
            aroma=8.5,
            acidity=8.0,
            sweetness=7.5,
            body=7.0,
            aftertaste=8.0,
            overall=8.0,
            flavor_notes=["citrus", "floral", "honey"],
            defects=[],
            rest_days=7,
            brew_method="V60",
            notes="Bright and clean with citrus notes",
        )
        cupping_id = cupping["id"]
        print(f"   Created cupping: {cupping_id}")
        print(f"   Total score: {cupping['total_score']} (max 60)")
        print(f"   Flavor notes: {cupping['flavor_notes']}")

        # Test get_cupping
        print("\n3. Getting cupping details...")
        details = await get_cupping(ctx, cupping_id)
        print(f"   Aroma: {details['aroma']}")
        print(f"   Acidity: {details['acidity']}")
        print(f"   Brew method: {details['brew_method']}")

        # Test list_cuppings
        print("\n4. Listing cuppings for roast...")
        listed = await list_cuppings(ctx, roast_id=roast_id)
        print(f"   Found {listed['total']} cupping(s)")
        for c in listed["cuppings"]:
            print(f"   - {c['id']}: score={c['total_score']}")

        # Test update_cupping
        print("\n5. Updating cupping (changing overall to 8.5)...")
        updated = await update_cupping(
            ctx,
            cupping_id,
            overall=8.5,
            notes="Revised: even better after resting",
        )
        print(f"   New total score: {updated['total_score']}")
        print(f"   Aroma preserved: {updated['aroma']}")  # Should still be 8.5
        print(f"   Updated notes: {updated['notes']}")

        # Test invalid roast_id
        print("\n6. Testing validation (invalid roast_id)...")
        try:
            await add_cupping(ctx, roast_id="invalid-uuid")
            print("   ERROR: Should have raised ValueError!")
        except ValueError as e:
            print(f"   Correctly rejected: {e}")

        # Test delete_cupping
        print("\n7. Deleting cupping...")
        result = await delete_cupping(ctx, cupping_id)
        print(f"   Deleted: {result['deleted']}")

        # Verify deletion
        print("\n8. Verifying deletion...")
        try:
            await get_cupping(ctx, cupping_id)
            print("   ERROR: Should have raised ValueError!")
        except ValueError as e:
            print(f"   Correctly deleted: {e}")

        print("\n" + "=" * 60)
        print("All tests passed!")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
