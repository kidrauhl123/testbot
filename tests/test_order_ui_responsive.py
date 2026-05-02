import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_TEMPLATE = PROJECT_ROOT / "templates" / "index.html"
ADMIN_TEMPLATE = PROJECT_ROOT / "templates" / "admin.html"


class OrderUIResponsiveTests(unittest.TestCase):
    def read_template(self, path):
        return path.read_text(encoding="utf-8")

    def test_index_order_view_defaults_to_mobile_card_mode(self):
        source = self.read_template(INDEX_TEMPLATE)

        self.assertIn("let viewMode = 'grid';", source)
        self.assertRegex(
            source,
            r'id="gridViewBtn"[^>]*class="[^"]*active[^"]*"',
            "grid/card view button should be active when JS defaults to grid view",
        )
        self.assertNotRegex(
            source,
            r'id="tableViewBtn"[^>]*class="[^"]*active[^"]*"',
            "table view button must not be initially active when grid is the default",
        )

    def test_index_order_cards_expose_full_action_group(self):
        source = self.read_template(INDEX_TEMPLATE)

        self.assertIn('class="order-actions"', source)
        grid_block = re.search(r"function renderGridView\(container, orders\) \{(?P<body>.*?)function renderTableView", source, re.S)
        self.assertIsNotNone(grid_block, "renderGridView should remain present before renderTableView")
        body = grid_block.group("body")

        for marker in (
            "cancelOrder(${o.id}, this)",
            "showOrderDetail(${o.id})",
            "disputeOrder(${o.id})",
            "urgeOrder(${o.id}, this)",
            "canRequestUpdate(o)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_index_has_mobile_rules_for_order_controls_and_tables(self):
        source = self.read_template(INDEX_TEMPLATE)

        self.assertRegex(source, r"@media\s*\(max-width:\s*600px\)")
        for marker in (
            ".header-actions",
            ".search-box",
            ".order-actions",
            ".orders-table-wrapper",
            ".orders-table",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_admin_order_toolbar_uses_responsive_classes_not_inline_widths(self):
        source = self.read_template(ADMIN_TEMPLATE)

        self.assertIn('class="admin-order-toolbar"', source)
        self.assertIn('class="form-control admin-order-search"', source)
        self.assertNotIn('id="order-search" class="form-control" placeholder="搜索订单ID、账号、创建者..." style="width: 250px;"', source)

    def test_admin_order_rows_include_mobile_data_labels_and_action_group(self):
        source = self.read_template(ADMIN_TEMPLATE)

        for marker in (
            'data-label="订单ID"',
            'data-label="账号"',
            'data-label="套餐"',
            'data-label="状态"',
            'data-label="创建者"',
            'data-label="接单人"',
            'data-label="创建时间"',
            'data-label="操作"',
            'class="admin-order-actions"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_admin_mobile_css_stacks_order_table_and_modal_actions(self):
        source = self.read_template(ADMIN_TEMPLATE)

        self.assertRegex(source, r"@media\s*\(max-width:\s*600px\)")
        for marker in (
            ".admin-order-toolbar",
            ".admin-order-search",
            "#orders-table-container .data-table thead",
            "#orders-table-container .data-table tr",
            "#orders-table-container .data-table td::before",
            ".admin-order-actions",
            ".modal-footer",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
