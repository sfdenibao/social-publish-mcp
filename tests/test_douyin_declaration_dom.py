"""Local browser fixtures only: no account profile, upload, or platform request."""
import html
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from patchright.async_api import async_playwright
from uploader.douyin_uploader.main import DouYinVideo
from utils.publish_errors import DeclarationError


OPTIONS = ['内容由AI生成', '内容为转载信息', '内容为个人观点或见解']


class DeclarationDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        chrome = os.environ.get('SAU_CHROME_PATH', 'C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not Path(chrome).is_file():
            self.skipTest('Installed Chrome required for local DOM fixture')
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path=chrome, headless=True)
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.abort())
        self.video = DouYinVideo('fixture', 'unused.mp4', [], 0, 'unused.json')
        self.video._clear_blocking_overlays = AsyncMock()

    async def asyncTearDown(self):
        if hasattr(self, 'browser'):
            await self.browser.close()
            await self.playwright.stop()

    async def fixture(self, options=OPTIONS, *, close=True, disabled=False):
        radios = ''.join(
            '<label class="semi-radio"><input type="radio" name="decl" '
            + ('disabled ' if disabled else '')
            + 'value="' + str(index) + '"><span class="semi-radio-addon">'
            + html.escape(option) + '</span></label>'
            for index, option in enumerate(options)
        )
        await self.page.set_content('''
            <button onclick="document.querySelector('#dialog').style.display='block'">请选择自主声明</button>
            <button onclick="document.body.dataset.submits=Number(document.body.dataset.submits)+1">发布</button>
            <div class="semi-modal-content" style="display:none">请选择声明类型（单选）</div>
            <div id="dialog" class="semi-modal-content" style="display:none">
              <h2>请选择声明类型（单选）</h2>''' + radios + '''
              <button class="semi-button-primary" onclick="document.body.dataset.confirms=Number(document.body.dataset.confirms)+1;
                document.body.dataset.choice=document.querySelector('input:checked')?.value;'''
            + ("document.querySelector('#dialog').style.display='none';" if close else '')
            + '''">确定</button></div>
            <script>document.body.dataset.submits=0;document.body.dataset.confirms=0;</script>''')

    async def counters(self):
        return await self.page.evaluate('[Number(document.body.dataset.confirms),Number(document.body.dataset.submits)]')

    async def semi_fixture(self, *, mode='correct'):
        """Observed Semi shape: selection renders but input.checked stays false."""
        radios = ''.join(
            '<div><label class="semi-radio"><span class="semi-radio-inner">'
            '<input type="radio" name="default"><span class="semi-radio-inner-display"></span>'
            '</span><span class="semi-radio-addon">' + html.escape(option) + '</span></label></div>'
            for option in OPTIONS
        )
        await self.page.set_content('''
            <button onclick="document.querySelector('#dialog').style.display='block'">请选择自主声明</button>
            <button onclick="document.body.dataset.submits=Number(document.body.dataset.submits)+1">发布</button>
            <div id="dialog" class="semi-modal-content" style="display:none">
              <h2>请选择声明类型（单选）</h2>
              <div class="declaratonBar-fixture">作者声明：</div>''' + radios + '''
              <button class="semi-button-primary" onclick="document.body.dataset.confirms=Number(document.body.dataset.confirms)+1;
                document.body.dataset.choice=document.querySelector('.semi-radio-checked .semi-radio-addon')?.textContent;
                document.querySelector('#dialog').style.display='none';">确定</button></div>
            <script>
              document.body.dataset.submits=0;document.body.dataset.confirms=0;
              document.querySelectorAll('label').forEach(label => label.addEventListener('click', event => {
                event.preventDefault();
                const all=[...document.querySelectorAll('label')];
                const mode=document.body.dataset.mode;
                const other=all.find(item => item!==label);
                const selected=mode==='wrong-label' ? other : label;
                selected.classList.add('semi-radio-checked');
                if(mode!=='missing-inner') selected.querySelector('.semi-radio-inner').classList.add('semi-radio-inner-checked');
                if(mode==='multiple-labels') other.classList.add('semi-radio-checked');
                if(mode==='multiple-inners') other.querySelector('.semi-radio-inner').classList.add('semi-radio-inner-checked');
                if(mode==='contradictory-native') other.querySelector('input').checked=true;
                const preview=document.querySelector('.declaratonBar-fixture');
                preview.textContent='作者声明：'+(mode==='wrong-preview' ? '错误声明' : label.querySelector('.semi-radio-addon').textContent);
                if(mode==='missing-preview') preview.remove();
              }));
            </script>''')
        await self.page.locator('body').evaluate('(body, mode) => body.dataset.mode=mode', mode)

    async def test_all_three_exact_options_are_selected_and_confirmed(self):
        for index, option in enumerate(OPTIONS):
            with self.subTest(option=option):
                await self.fixture()
                self.assertTrue(await self.video.set_self_declaration(self.page, option))
                self.assertEqual(await self.page.locator('body').get_attribute('data-choice'), str(index))
                self.assertEqual(await self.counters(), [1, 0])

    async def test_all_three_semi_uncontrolled_options_need_exact_preview(self):
        for option in OPTIONS:
            with self.subTest(option=option):
                await self.semi_fixture()
                self.assertTrue(await self.video.set_self_declaration(self.page, option))
                self.assertEqual(await self.page.locator('body').get_attribute('data-choice'), option)
                self.assertEqual(await self.page.locator('input:checked').count(), 0)
                self.assertEqual(await self.counters(), [1, 0])

    async def test_semi_fallback_rejects_incomplete_or_contradictory_selection(self):
        for mode in ['wrong-label', 'multiple-labels', 'missing-inner', 'multiple-inners',
                     'missing-preview', 'wrong-preview', 'contradictory-native']:
            with self.subTest(mode=mode):
                await self.semi_fixture(mode=mode)
                with self.assertRaises(DeclarationError) as caught:
                    await self.video.set_self_declaration(self.page, OPTIONS[2])
                self.assertEqual(caught.exception.code, 'DECLARATION_SELECTION_FAILED')
                self.assertEqual(caught.exception.details['available_options'], OPTIONS)
                self.assertEqual(await self.counters(), [0, 0])

    async def test_aria_radio_selection_still_confirms(self):
        await self.fixture()
        await self.page.locator('label').evaluate_all('''labels => labels.forEach(label => {
            label.querySelector('input').remove();
            const radio=document.createElement('span');
            radio.setAttribute('role','radio');radio.setAttribute('aria-checked','false');
            label.append(radio);
            label.onclick=()=>radio.setAttribute('aria-checked','true');
        })''')
        self.assertTrue(await self.video.set_self_declaration(self.page, OPTIONS[2]))
        self.assertEqual(await self.counters(), [1, 0])

    async def test_original_missing_reports_available_options_without_confirming(self):
        await self.fixture()
        with self.assertRaises(DeclarationError) as caught:
            await self.video.set_self_declaration(self.page, '原创')
        self.assertEqual(caught.exception.code, 'DECLARATION_OPTION_NOT_FOUND')
        self.assertEqual(caught.exception.details['available_options'], OPTIONS)
        self.assertEqual(await self.counters(), [0, 0])

    async def test_no_substring_selection_or_selector_interpolation(self):
        for value in ['内容', '原创', '\"), input, :text-is(\"']:
            with self.subTest(value=value):
                await self.fixture(['非原创', *OPTIONS])
                with self.assertRaises(DeclarationError) as caught:
                    await self.video.set_self_declaration(self.page, value)
                self.assertEqual(caught.exception.code, 'DECLARATION_OPTION_NOT_FOUND')
                self.assertEqual(await self.page.locator('input:checked').count(), 0)

    async def test_ambiguous_same_label_is_not_selected(self):
        await self.fixture([OPTIONS[0], OPTIONS[0]])
        with self.assertRaises(DeclarationError) as caught:
            await self.video.set_self_declaration(self.page, OPTIONS[0])
        self.assertEqual(caught.exception.code, 'DECLARATION_SELECTION_FAILED')
        self.assertEqual(await self.counters(), [0, 0])

    async def test_confirmation_that_keeps_dialog_open_is_failure(self):
        await self.fixture(close=False)
        with self.assertRaises(DeclarationError) as caught:
            await self.video.set_self_declaration(self.page, OPTIONS[0])
        self.assertEqual(caught.exception.code, 'DECLARATION_CONFIRM_FAILED')
        self.assertEqual(caught.exception.details['available_options'], OPTIONS)
        self.assertEqual(await self.counters(), [1, 0])

    async def test_disabled_radio_never_confirms(self):
        await self.fixture(disabled=True)
        with self.assertRaises(DeclarationError) as caught:
            await self.video.set_self_declaration(self.page, OPTIONS[0])
        self.assertEqual(caught.exception.code, 'DECLARATION_SELECTION_FAILED')
        self.assertEqual(caught.exception.details['available_options'], OPTIONS)
        self.assertEqual(await self.counters(), [0, 0])


if __name__ == '__main__':
    unittest.main()
