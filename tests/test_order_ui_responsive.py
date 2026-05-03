import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_TEMPLATE = PROJECT_ROOT / "templates" / "index.html"
ADMIN_TEMPLATE = PROJECT_ROOT / "templates" / "admin.html"


class OrderUIResponsiveTests(unittest.TestCase):
    def read_template(self, path):
        return path.read_text(encoding="utf-8")

    def test_index_order_view_is_card_only_without_switcher(self):
        source = self.read_template(INDEX_TEMPLATE)

        for removed_marker in (
            "view-toggle",
            "gridViewBtn",
            "tableViewBtn",
            "toggleViewMode",
            "viewMode",
            "renderTableView",
            "orders-table-wrapper",
            "orders-table",
        ):
            with self.subTest(removed_marker=removed_marker):
                self.assertNotIn(removed_marker, source)

        self.assertIn("function renderOrderCards(container, orders)", source)
        self.assertIn("renderOrderCards(container, ordersToShow);", source)

    def test_index_order_cards_expose_full_action_group(self):
        source = self.read_template(INDEX_TEMPLATE)

        self.assertIn('class="order-actions"', source)
        card_block = re.search(r"function renderOrderCards\(container, orders\) \{(?P<body>.*?)function loadMoreOrders", source, re.S)
        self.assertIsNotNone(card_block, "renderOrderCards should be the only index order renderer")
        body = card_block.group("body")

        for marker in (
            "cancelOrder(${o.id}, this)",
            "showOrderDetail(${o.id})",
            "disputeOrder(${o.id})",
            "urgeOrder(${o.id}, this)",
            "canRequestUpdate(o)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_index_has_mobile_rules_for_order_controls_and_cards(self):
        source = self.read_template(INDEX_TEMPLATE)

        self.assertRegex(source, r"@media\s*\(max-width:\s*600px\)")
        for marker in (
            ".header-actions",
            ".search-box",
            ".order-actions",
            ".order-list-container",
            ".order-list-item",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_index_homepage_core_layout_is_mobile_first(self):
        source = self.read_template(INDEX_TEMPLATE)
        start = source.find("@media (max-width: 600px)")
        self.assertNotEqual(start, -1, "index should have a focused 600px mobile media block")
        body = source[start:].split("</style>", 1)[0]

        for marker in (
            "body",
            ".main-container",
            ".container",
            ".left, .right",
            ".navbar",
            ".navbar-user",
            ".balance-badge",
            ".card",
            ".card-header",
            ".submit-btn",
            ".form-control",
            ".price-display",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

        self.assertIn("overflow-x: hidden", body)
        self.assertIn("margin: 12px auto", body)
        self.assertIn("padding: 0 10px", body)
        self.assertIn("flex-direction: column", body)
        self.assertIn("width: 100%", body)
        self.assertIn("font-size: 16px", body)

    def test_index_homepage_markup_has_mobile_layout_hooks(self):
        source = self.read_template(INDEX_TEMPLATE)

        for marker in (
            'class="card order-form-card"',
            'class="card orders-panel-card"',
            'class="form-actions"',
            'class="mobile-safe-text"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_index_mobile_top_bar_uses_readable_separated_rows(self):
        source = self.read_template(INDEX_TEMPLATE)
        start = source.find("@media (max-width: 600px)")
        self.assertNotEqual(start, -1, "index should have a focused 600px mobile media block")
        body = source[start:].split("</style>", 1)[0]

        for marker in (
            'class="navbar-account-row"',
            'class="navbar-balance-row"',
            ".navbar-account-row",
            ".navbar-balance-row",
            "min-height: 48px",
            "padding: 10px 12px",
            "background: rgba(255,255,255,0.16)",
            ".recharge-btn",
            "min-height: 44px",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source if marker.startswith('class=') else body)

    def test_index_mobile_order_panel_header_avoids_crowded_single_line(self):
        source = self.read_template(INDEX_TEMPLATE)
        start = source.find("@media (max-width: 600px)")
        self.assertNotEqual(start, -1, "index should have a focused 600px mobile media block")
        body = source[start:].split("</style>", 1)[0]

        for marker in (
            ".orders-panel-card .card-header",
            ".orders-panel-card .header-actions",
            "display: grid",
            "grid-template-columns: 1fr",
            ".orders-panel-card .today-total",
            ".refresh-hint",
            "text-align: left",
            "line-height: 1.45",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_index_mobile_order_cards_wrap_long_accounts_and_reduce_density(self):
        source = self.read_template(INDEX_TEMPLATE)
        start = source.find("@media (max-width: 600px)")
        self.assertNotEqual(start, -1, "index should have a focused 600px mobile media block")
        body = source[start:].split("</style>", 1)[0]

        for marker in (
            ".order-list-item",
            ".order-list-item p",
            ".order-list-item p.account",
            "overflow-wrap: anywhere",
            "word-break: break-word",
            "line-height: 1.55",
            ".status-badge",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_index_balance_refresh_preserves_mobile_recharge_link(self):
        source = self.read_template(INDEX_TEMPLATE)
        self.assertIn('id="balanceBadge"', source)
        self.assertIn('class="recharge-btn"', source)
        self.assertIn("const balanceText = balanceBadge.querySelector('.mobile-safe-text');", source)
        self.assertIn("balanceText.textContent = `余额: ${balance}元${creditLimit > 0 ? ` (额度: ${creditLimit}元)` : ''}`;", source)
        self.assertNotIn("balanceBadge.textContent = `余额:", source)

    def test_index_mobile_form_fields_use_phone_readable_type_sizes(self):
        source = self.read_template(INDEX_TEMPLATE)
        start = source.find("@media (max-width: 600px)")
        self.assertNotEqual(start, -1, "index should have a focused 600px mobile media block")
        body = source[start:].split("</style>", 1)[0]

        for marker in (
            "font-size: 16px",
            "font-size: 17px",
            "min-height: 50px",
            "padding: 13px 14px",
            ".form-group label",
            ".form-control",
            ".search-input",
            ".price-display",
            ".price-display small",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

        self.assertNotIn('font-size:12px', source)

    def test_index_mobile_order_cards_use_phone_readable_type_sizes(self):
        source = self.read_template(INDEX_TEMPLATE)
        start = source.find("@media (max-width: 600px)")
        self.assertNotEqual(start, -1, "index should have a focused 600px mobile media block")
        body = source[start:].split("</style>", 1)[0]

        for marker in (
            ".order-list-item {",
            "font-size: 16px",
            ".order-list-item h4",
            "font-size: 19px",
            ".order-list-item p",
            ".order-list-item p.price",
            ".order-list-item p.time",
            ".failed-reason",
            ".status-badge",
            "font-size: 15px",
            "line-height: 1.6",
            ".order-actions .detail-btn",
            "min-height: 44px",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

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
    def test_admin_mobile_order_controls_are_phone_sized(self):
        source = self.read_template(ADMIN_TEMPLATE)
        start = source.find("@media (max-width: 600px)")
        self.assertNotEqual(start, -1, "admin should have focused 600px mobile order rules")
        body = source[start:].split("</style>", 1)[0]

        for marker in (
            ".admin-order-toolbar",
            "position: sticky",
            ".admin-order-search",
            "font-size: 17px",
            "min-height: 48px",
            ".admin-order-toolbar .btn",
            "min-height: 48px",
            "#orders-table-container .data-table td",
            "font-size: 15px",
            "min-height: 48px",
            "padding: 12px 12px 12px 112px",
            "#orders-table-container .data-table td::before",
            "width: 92px",
            ".admin-order-actions .btn",
            "min-height: 44px",
            ".modal-footer .btn",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

        self.assertNotIn("font-size: 12px", body)
        self.assertNotIn("min-height: 38px", body)
        self.assertNotIn("min-height: 40px", body)

    def test_order_loading_uses_smaller_paginated_payloads(self):
        index_source = self.read_template(INDEX_TEMPLATE)
        admin_source = self.read_template(ADMIN_TEMPLATE)

        self.assertIn("fetch('/orders/recent?limit=50')", index_source)
        self.assertNotIn("/orders/recent?limit=200", index_source)
        self.assertIn("const adminOrderPageSize = 50;", admin_source)
        self.assertIn("params.set('limit', adminOrderPageSize);", admin_source)
        self.assertIn("params.set('offset', adminOrderOffset);", admin_source)
        self.assertIn("const searchValue = orderSearchInput ? orderSearchInput.value.trim() : '';", admin_source)
        self.assertIn("fetch(`/admin/api/orders?${params.toString()}`)", admin_source)
        self.assertIn("renderAdminOrderPagination(total, orders.length);", admin_source)
        self.assertNotIn("limit=1000", admin_source)
        self.assertNotIn('console.log("成功获取订单数据:", orders);', admin_source)


if __name__ == "__main__":
    unittest.main()
