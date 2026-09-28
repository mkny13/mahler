const { chromium } = require('./.calendar-tools/node_modules/playwright');
(async () => {
 const browser = await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 for (const layout of ['desktop','phone']) {
  const page = await browser.newPage({viewport:layout==='desktop'?{width:1440,height:1700}:{width:390,height:844},colorScheme:'light'});
  await page.goto('file://'+process.cwd()+'/.calendar-'+layout+'.html');
  await page.locator('.weekly-quota').scrollIntoViewIfNeeded();
  const box=await page.locator('.weekly-quota').boundingBox();
  console.log(layout,JSON.stringify(box));
  if(layout==='desktop') await page.screenshot({path:'docs/console/screens/554-calendar-desktop.png'});
  else await page.locator('.weekly-quota').screenshot({path:'docs/console/screens/554-calendar-phone.png'});
  const hour=page.locator('.weekly-hour summary').first();
  await hour.click();
  if(!await page.locator('.weekly-hour[open] .weekly-tip').isVisible()) throw Error('Hour details did not open');
  console.log(layout,'hour tap opens route text');
  await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
