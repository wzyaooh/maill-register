import random
import numpy as np
import logging

logger = logging.getLogger('gmail_creator_behavior')

class HumanBehavior:
    """Simulates advanced human behaviors like non-linear mouse movements and organic typing with typos"""
    
    @staticmethod
    def generate_bezier_curve(start_coords, end_coords, num_points=20):
        """Generates a natural, slightly curved path between two points"""
        x0, y0 = start_coords
        x3, y3 = end_coords
        
        # Add some random deviation for control points
        deviation_x = abs(x3 - x0) * 0.3
        deviation_y = abs(y3 - y0) * 0.3
        
        x1 = x0 + (x3 - x0) * 0.3 + random.uniform(-deviation_x, deviation_x)
        y1 = y0 + (y3 - y0) * 0.3 + random.uniform(-deviation_y, deviation_y)
        
        x2 = x0 + (x3 - x0) * 0.7 + random.uniform(-deviation_x, deviation_x)
        y2 = y0 + (y3 - y0) * 0.7 + random.uniform(-deviation_y, deviation_y)
        
        points = []
        for t in np.linspace(0, 1, num_points):
            xt = (1-t)**3 * x0 + 3 * (1-t)**2 * t * x1 + 3 * (1-t) * t**2 * x2 + t**3 * x3
            yt = (1-t)**3 * y0 + 3 * (1-t)**2 * t * y1 + 3 * (1-t) * t**2 * y2 + t**3 * y3
            points.append((int(xt), int(yt)))
        
        return points

    @staticmethod
    async def natural_type(page, selector, text, make_typos=True):
        """Types text with variable speeds and optional typos and backspaces"""
        try:
            element = await page.wait_for_selector(selector, timeout=5000)
            if not element:
                return False
                
            await element.click()
            await page.wait_for_timeout(random.randint(100, 300))
            
            for i, char in enumerate(text):
                # Generate a typo randomly (2% chance) if enabled
                if make_typos and random.random() < 0.005 and char.isalpha():
                    # Pick an adjacent key conceptually or random letter
                    typo = random.choice('qwertyuiopasdfghjklzxcvbnm')
                    await page.keyboard.type(typo)
                    await page.wait_for_timeout(random.randint(200, 400))
                    # Notice typo and backspace
                    await page.keyboard.press('Backspace')
                    await page.wait_for_timeout(random.randint(100, 200))
                
                # Type correct char
                await page.keyboard.type(char)
                
                # Natural typing speed variation (faster in middle of words, slower at start/end)
                delay = random.uniform(30, 120)
                if i == 0 or i == len(text) - 1:
                    delay += random.uniform(50, 150)
                await page.wait_for_timeout(int(delay))
                
            return True
        except Exception as e:
            logger.error(f"Natural typing failed on {selector}: {e}")
            return False

    @staticmethod
    async def human_scroll(page, min_scrolls=1, max_scrolls=3):
        """Scrolls the page randomly simulating a reading human"""
        scrolls = random.randint(min_scrolls, max_scrolls)
        for _ in range(scrolls):
            direction = 1 if random.random() > 0.2 else -1 # 80% down, 20% up
            distance = random.randint(100, 500) * direction
            await page.mouse.wheel(0, distance)
            await page.wait_for_timeout(random.randint(800, 2000))
