import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import i18n from '../i18n';
import { ProcessOverview } from './ProcessOverview';

describe('ProcessOverview', () => {
  it('shows the available pipeline and the stages in development', () => {
    render(<ProcessOverview />);
    expect(screen.getByRole('region', { name: 'How a lesion becomes a research report' })).toBeInTheDocument();
    expect(screen.getByText(/flnm-fast/)).toBeInTheDocument();
    expect(screen.getByText(/lnm-tracts/)).toBeInTheDocument();
    expect(screen.getByText('COGNET cognitive profile')).toBeInTheDocument();
    expect(screen.getByText(/PDF report/)).toBeInTheDocument();
    const planned = screen.getAllByRole('listitem').filter((li) => li.getAttribute('aria-disabled') === 'true');
    expect(planned).toHaveLength(4);
    for (const item of planned) expect(within(item).getByText('In development')).toBeInTheDocument();
  });

  it('is available in Spanish', async () => {
    await i18n.changeLanguage('es');
    render(<ProcessOverview />);
    // one section heading plus one badge per stage
    expect(screen.getAllByText('En desarrollo')).toHaveLength(5);
  });
});
